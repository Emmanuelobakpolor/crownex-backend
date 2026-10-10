"""API views for the CrownEx admin panel.

Two entry points are open (create + login); everything else requires an
authenticated staff account (IsAdminUser -> request.user.is_staff).
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.services import delete_user, issue_tokens
from cards.models import VirtualCard
from crypto import orders as crypto_orders
from crypto import quidax as crypto_quidax
from crypto import services as crypto_services
from crypto import withdrawals as crypto_withdrawals
from crypto.models import CryptoFeeSettings, CryptoOrder, CryptoWallet, CryptoWithdrawal
from crypto.serializers import CryptoFeeSettingsSerializer, CryptoFeeSettingsUpdateSerializer
from kyc import services as kyc_services
from kyc.models import KycVerification
from wallet.models import Wallet

from . import services
from .serializers import (
    AdminCryptoOrderActionSerializer,
    AdminCryptoOrderSerializer,
    AdminCryptoWithdrawalActionSerializer,
    AdminCryptoWithdrawalSerializer,
    AdminKycActionSerializer,
    AdminKycVerificationSerializer,
    AdminLoginSerializer,
    AdminProfileUpdateSerializer,
    AdminVirtualCardSerializer,
    CreateAdminSerializer,
)


class CreateAdminView(APIView):
    """POST /api/admin/create/ — bootstrap an admin account from Postman.

    There's no admin session to gate this behind when the very first admin
    is being created, so it's gated by a shared secret instead: set
    ADMIN_REGISTRATION_SECRET in the backend's environment, then send the
    same value as the X-Admin-Secret header.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        configured_secret = settings.ADMIN_REGISTRATION_SECRET
        if not configured_secret:
            return Response(
                {'detail': 'Admin registration is not configured on this server.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if request.headers.get('X-Admin-Secret') != configured_secret:
            return Response(
                {'detail': 'Invalid admin secret.'}, status=status.HTTP_403_FORBIDDEN
            )

        serializer = CreateAdminSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = User.objects.create_superuser(
            email=data['email'],
            password=data['password'],
            full_name=data.get('full_name', ''),
        )

        return Response(
            {
                'message': 'Admin account created. Log in at POST /api/admin/login/.',
                'email': user.email,
                'full_name': user.full_name,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminLoginView(APIView):
    """POST /api/admin/login/ — email + password -> JWT, staff accounts only."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = AdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_staff:
            return Response(
                {'detail': 'This account does not have admin access.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response(
                {'detail': 'This account has been deactivated.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        authenticated = authenticate(username=email, password=password)
        if authenticated is None and not user.check_password(password):
            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        tokens = issue_tokens(user)
        return Response(
            {
                'message': 'Login successful.',
                'admin': {
                    'id': user.id,
                    'email': user.email,
                    'full_name': user.full_name,
                },
                'access': tokens['access'],
                'refresh': tokens['refresh'],
            }
        )


class AdminOverviewView(APIView):
    """GET /api/admin/overview/ — dashboard stat cards."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        return Response(services.get_overview_stats())


class AdminUserListView(APIView):
    """GET /api/admin/users/?search=&page=&page_size= — paginated user list."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        search = request.query_params.get('search', '').strip()
        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)
        page = request.query_params.get('page', 1)

        paginator = Paginator(services.get_users(search), page_size)
        page_obj = paginator.get_page(page)

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'results': [
                    services.serialize_user(u, request=request) for u in page_obj.object_list
                ],
            }
        )


class AdminUserDetailView(APIView):
    """GET /api/admin/users/<id>/ — full detail for the user-detail panel."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        try:
            user = services.get_users().get(pk=pk)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(services.serialize_user(user, request=request))

    def delete(self, request, pk):
        """Permanently delete a user and everything cascading from them."""
        try:
            user = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        if user.pk == request.user.pk:
            return Response(
                {'detail': 'You cannot delete your own admin account.', 'code': 'self_delete'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_staff or user.is_superuser:
            return Response(
                {
                    'detail': 'Staff accounts cannot be deleted through this endpoint.',
                    'code': 'staff_protected',
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        snapshot = delete_user(user, deleted_by=request.user)
        return Response(
            {'message': f'User {snapshot["email"]} permanently deleted.', 'deleted': snapshot},
            status=status.HTTP_200_OK,
        )


class AdminTransactionListView(APIView):
    """GET /api/admin/transactions/?type=&page=&page_size= — unified feed.

    type is one of: all, deposit, withdrawal, airtime, data, cable,
    electricity, vtu (all VTU services), giftcard.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        tx_type = request.query_params.get('type', 'all')
        page = int(request.query_params.get('page', 1) or 1)
        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)

        items = services.get_unified_transactions(tx_type)
        start = (page - 1) * page_size
        end = start + page_size

        return Response(
            {
                'count': len(items),
                'page': page,
                'page_size': page_size,
                'results': items[start:end],
            }
        )


class AdminProfileView(APIView):
    """GET/PATCH /api/admin/profile/ — the logged-in admin's own account."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        return Response(
            {
                'id': request.user.id,
                'email': request.user.email,
                'full_name': request.user.full_name,
            }
        )

    def patch(self, request):
        serializer = AdminProfileUpdateSerializer(
            data=request.data, context={'user': request.user}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        if data.get('new_email'):
            user.email = data['new_email']
        if 'full_name' in data:
            user.full_name = data['full_name']
        user.save()

        return Response(
            {
                'message': 'Profile updated successfully.',
                'id': user.id,
                'email': user.email,
                'full_name': user.full_name,
            }
        )


class AdminCryptoFeeListView(APIView):
    """GET /api/admin/fees/ — all crypto fee rows (auto-creates buy/sell/swap/withdraw)."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        rows = crypto_services.get_all_fee_settings()
        return Response(CryptoFeeSettingsSerializer(rows, many=True).data)


class AdminCryptoFeeUpdateView(APIView):
    """PATCH /api/admin/fees/<id>/ — update flat_usd, percent, is_active.

    Only affects quotes created after the save — any quote already locked
    keeps its frozen fee (see CryptoQuote in a later phase).
    """

    permission_classes = [permissions.IsAdminUser]

    def patch(self, request, pk):
        try:
            row = CryptoFeeSettings.objects.get(pk=pk)
        except CryptoFeeSettings.DoesNotExist:
            return Response({'detail': 'Fee setting not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = CryptoFeeSettingsUpdateSerializer(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(CryptoFeeSettingsSerializer(row).data)


class AdminCryptoOrderListView(APIView):
    """GET /api/admin/crypto/orders/?status=&type=&needs_review=1&search=&page=&page_size="""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = CryptoOrder.objects.select_related('user').prefetch_related('logs').order_by('-created_at')

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        type_filter = request.query_params.get('type')
        if type_filter:
            qs = qs.filter(order_type=type_filter)
        if request.query_params.get('needs_review') in ('1', 'true'):
            qs = qs.filter(needs_review=True)
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(reference__icontains=search) | Q(user__email__icontains=search))

        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)
        page = request.query_params.get('page', 1)
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(page)

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'results': AdminCryptoOrderSerializer(
                    page_obj.object_list, many=True, context={'request': request}
                ).data,
            }
        )


class AdminCryptoOrderActionView(APIView):
    """POST /api/admin/crypto/orders/<reference>/ — { action, note? }

    action is one of:
      approve         bank-transfer buy with proof uploaded -> execute
      reject          cancel a stuck pending_payment/waiting_deposit order
      confirm_deposit waiting_deposit sell -> re-attempt (webhook missed)
      retry           re-attempt a failed buy
      confirm_executed      order held for review: Quidax DID execute it -> finish it
      confirm_not_executed  order held for review: Quidax did NOT -> refund/release
      swap_credit_coin      failed swap, sell leg done: credit the promised destination coin
      swap_refund_ngn       failed swap, sell leg done: credit its net NGN value instead
    """

    permission_classes = [permissions.IsAdminUser]

    ACTIONS = {
        'approve': crypto_orders.admin_approve_buy,
        'reject': crypto_orders.admin_reject_order,
        'confirm_deposit': crypto_orders.admin_confirm_sell_deposit,
        'retry': crypto_orders.admin_retry_buy,
        'confirm_executed': lambda order, note: crypto_orders.admin_resolve_unknown_order(
            order, note, executed=True
        ),
        'confirm_not_executed': lambda order, note: crypto_orders.admin_resolve_unknown_order(
            order, note, executed=False
        ),
        'swap_credit_coin': lambda order, note: crypto_orders.admin_resolve_failed_swap(
            order, note, credit='to_coin'
        ),
        'swap_refund_ngn': lambda order, note: crypto_orders.admin_resolve_failed_swap(
            order, note, credit='ngn'
        ),
    }

    def post(self, request, reference):
        try:
            order = CryptoOrder.objects.get(reference=reference)
        except CryptoOrder.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdminCryptoOrderActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = self.ACTIONS[data['action']](order, data['note'])
        except crypto_services.CryptoServiceError as exc:
            return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)

        return Response(AdminCryptoOrderSerializer(order, context={'request': request}).data)


class AdminCryptoWithdrawalListView(APIView):
    """GET /api/admin/crypto/withdrawals/?status=&search=&page=&page_size="""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = (
            CryptoWithdrawal.objects.select_related('user')
            .prefetch_related('logs')
            .order_by('-created_at')
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(reference__icontains=search) | Q(user__email__icontains=search))

        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)
        page = request.query_params.get('page', 1)
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(page)

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'results': AdminCryptoWithdrawalSerializer(page_obj.object_list, many=True).data,
            }
        )


class AdminCryptoWithdrawalActionView(APIView):
    """POST /api/admin/crypto/withdrawals/<reference>/ — { action, tx_id?, note? }

    action is one of: complete, reject — manual recovery for when a
    withdraw.successful/withdraw.rejected webhook never arrived.
    """

    permission_classes = [permissions.IsAdminUser]

    def post(self, request, reference):
        try:
            withdrawal = CryptoWithdrawal.objects.get(reference=reference)
        except CryptoWithdrawal.DoesNotExist:
            return Response({'detail': 'Withdrawal not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdminCryptoWithdrawalActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            if data['action'] == 'complete':
                withdrawal = crypto_withdrawals.admin_complete_withdrawal(
                    withdrawal, data['tx_id'], data['note']
                )
            else:
                withdrawal = crypto_withdrawals.admin_reject_withdrawal(withdrawal, data['note'])
        except crypto_services.CryptoServiceError as exc:
            return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)

        return Response(AdminCryptoWithdrawalSerializer(withdrawal).data)


class AdminKycListView(APIView):
    """GET /api/admin/kyc/?status=&search=&page=&page_size="""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = KycVerification.objects.select_related('user').prefetch_related('logs').order_by(
            '-created_at'
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(id_number__icontains=search) | Q(user__email__icontains=search))

        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)
        page = request.query_params.get('page', 1)
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(page)

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'results': AdminKycVerificationSerializer(
                    page_obj.object_list, many=True, context={'request': request}
                ).data,
            }
        )


class AdminKycActionView(APIView):
    """POST /api/admin/kyc/<id>/ — { action, note? }

    action is one of: approve, reject — manual override for verifications
    sitting in `review` (match score in the 70-89 gray zone) or to correct
    an auto-decided outcome.
    """

    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        try:
            verification = KycVerification.objects.get(pk=pk)
        except KycVerification.DoesNotExist:
            return Response({'detail': 'Verification not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdminKycActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            if data['action'] == 'approve':
                verification = kyc_services.admin_approve(verification, data['note'])
            else:
                verification = kyc_services.admin_reject(verification, data['note'])
        except kyc_services.KycServiceError as exc:
            return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)

        return Response(
            AdminKycVerificationSerializer(verification, context={'request': request}).data
        )


class AdminCardListView(APIView):
    """GET /api/admin/cards/?status=&search= — read-only visibility.

    Cards move through Bitnob's own lifecycle automatically (no manual
    approve/reject step like KYC or crypto orders) — this is for
    visibility and manual recovery, not routine action.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = (
            VirtualCard.objects.select_related('user')
            .prefetch_related('logs', 'transactions')
            .order_by('-created_at')
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(masked_pan__icontains=search) | Q(user__email__icontains=search))

        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)
        page = request.query_params.get('page', 1)
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(page)

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'results': AdminVirtualCardSerializer(page_obj.object_list, many=True).data,
            }
        )


class AdminMasterBalancesView(APIView):
    """GET /api/admin/crypto/master-balances/

    What the Quidax master account actually holds, per coin, next to what
    our internal ledger says users own (CryptoWallet available + reserved).
    A negative difference means user balances aren't fully backed — swaps
    and withdrawals of that coin will fail with 'insufficient balance'.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        try:
            wallets = crypto_quidax.list_wallets(settings.QUIDAX_USER_ID)
        except crypto_quidax.QuidaxError as exc:
            return Response(
                {'detail': f'Could not reach Quidax: {exc.message}'}, status=status.HTTP_502_BAD_GATEWAY
            )
        on_quidax = {
            str(w.get('currency') or '').lower(): w for w in wallets if isinstance(w, dict)
        }

        owed = {
            row['coin']: row
            for row in CryptoWallet.objects.values('coin').annotate(
                available=Sum('available'), reserved=Sum('reserved')
            )
        }

        def dec(value) -> Decimal:
            try:
                return Decimal(str(value or '0'))
            except (InvalidOperation, ValueError):
                return Decimal('0')

        rows = []
        coins = ['ngn', *crypto_services.SUPPORTED_COINS]
        coins += sorted(c for c in on_quidax if c not in coins and dec(on_quidax[c].get('balance')) > 0)
        for coin in coins:
            q = on_quidax.get(coin, {})
            balance, locked = dec(q.get('balance')), dec(q.get('locked'))
            if coin == 'ngn':
                users_available = Wallet.objects.aggregate(t=Sum('ngn_balance'))['t'] or Decimal('0')
                users_reserved = Decimal('0')
            else:
                o = owed.get(coin, {})
                users_available = o.get('available') or Decimal('0')
                users_reserved = o.get('reserved') or Decimal('0')
            users_total = users_available + users_reserved
            rows.append(
                {
                    'coin': coin,
                    'name': crypto_services.SUPPORTED_COINS.get(coin, {}).get('name')
                    or ('Naira' if coin == 'ngn' else coin.upper()),
                    'supported': coin in crypto_services.SUPPORTED_COINS,
                    'on_quidax': coin in on_quidax,
                    'master_balance': str(balance),
                    'master_locked': str(locked),
                    'users_available': str(users_available),
                    'users_reserved': str(users_reserved),
                    'users_total': str(users_total),
                    'difference': str(balance - users_total),
                }
            )

        return Response({'fetched_at': timezone.now().isoformat(), 'rows': rows})
