"""API views for the NGN wallet: deposit, withdraw, banks, and history."""

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import (
    InitiateDepositSerializer,
    ResolveAccountSerializer,
    TransactionSerializer,
    VerifyPaymentSerializer,
    WalletBalanceSerializer,
    WithdrawSerializer,
)


def _error_response(exc: services.WalletServiceError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


class WalletBalanceView(APIView):
    """GET /wallet/balance/"""

    def get(self, request):
        wallet = services.get_or_create_wallet(request.user)
        return Response(WalletBalanceSerializer(wallet).data)


class InitiateDepositView(APIView):
    """POST /wallet/initiate-deposit/"""

    def post(self, request):
        serializer = InitiateDepositSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.initiate_deposit(
                user=request.user,
                amount=serializer.validated_data['amount'],
                tx_ref=serializer.validated_data['tx_ref'],
            )
        except services.WalletServiceError as exc:
            return _error_response(exc)

        user = request.user
        return Response(
            {
                'tx_ref': tx.reference,
                'amount': str(tx.amount),
                'public_key': settings.FLW_PUBLIC_KEY,
                'customer': {
                    'email': user.email,
                    'phone': user.phone or '',
                    'name': user.full_name or user.email,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyPaymentView(APIView):
    """POST /wallet/verify-payment/"""

    def post(self, request):
        serializer = VerifyPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            wallet, tx = services.verify_payment(
                user=request.user, tx_ref=serializer.validated_data['tx_ref']
            )
        except services.WalletServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Payment verified. Wallet credited.',
                'ngn_balance': str(wallet.ngn_balance),
                'amount': str(tx.amount),
            }
        )


class FlutterwaveWebhookView(APIView):
    """POST /wallet/flw-webhook/ — public endpoint, authenticated by Verif-Hash."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        if request.headers.get('Verif-Hash') != settings.FLW_WEBHOOK_HASH:
            return Response({'detail': 'Invalid signature.'}, status=401)

        payload = request.data
        event = payload.get('event')
        data = payload.get('data') or {}

        if event == 'charge.completed' and data.get('status') == 'successful':
            services.handle_deposit_webhook(data.get('tx_ref'))
        elif event == 'transfer.completed':
            services.handle_transfer_webhook(data.get('reference'), data.get('status'))

        return Response({'status': 'ok'})


class BanksView(APIView):
    """GET /wallet/banks/"""

    def get(self, request):
        try:
            banks = services.list_banks()
        except services.WalletServiceError as exc:
            return _error_response(exc)
        return Response({'banks': banks})


class ResolveAccountView(APIView):
    """GET /wallet/resolve-account/?account_number=&bank_code="""

    def get(self, request):
        serializer = ResolveAccountSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        try:
            account_name = services.resolve_account_name(
                account_number=serializer.validated_data['account_number'],
                bank_code=serializer.validated_data['bank_code'],
            )
        except services.WalletServiceError as exc:
            return _error_response(exc)
        return Response({'account_name': account_name})


class WithdrawView(APIView):
    """POST /wallet/withdraw/"""

    def post(self, request):
        serializer = WithdrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.request_withdrawal(
                user=request.user, **serializer.validated_data
            )
        except services.WalletServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Withdrawal initiated. Processing within 24 hours.',
                'reference': tx.reference,
                'amount': str(tx.amount),
                'status': tx.status,
            },
            status=status.HTTP_201_CREATED,
        )


class TransactionHistoryView(APIView):
    """GET /wallet/transactions/ — latest 50 for the current user."""

    def get(self, request):
        transactions = services.list_transactions(request.user)
        return Response(TransactionSerializer(transactions, many=True).data)
