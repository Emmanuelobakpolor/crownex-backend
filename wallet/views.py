"""API views for the NGN wallet: deposit, withdraw, banks, and history."""

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import flutterwave, services
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
    """POST /wallet/initiate-deposit/ — creates a bank-transfer or USSD order."""

    def post(self, request):
        serializer = InitiateDepositSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.initiate_deposit(
                user=request.user,
                amount=serializer.validated_data['amount'],
                tx_ref=serializer.validated_data['tx_ref'],
                method=serializer.validated_data['method'],
                bank_code=serializer.validated_data.get('bank_code', ''),
            )
        except services.WalletServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'tx_ref': tx.reference,
                'amount': str(tx.amount),
                'method': tx.deposit_method,
                'account_number': tx.account_number,
                'bank_name': tx.bank_name,
                'expires_at': tx.virtual_account_expires_at,
                'note': tx.note,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyPaymentView(APIView):
    """POST /wallet/verify-payment/ — safe to call repeatedly while pending."""

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
                'status': tx.status,
                'ngn_balance': str(wallet.ngn_balance),
                'amount': str(tx.amount),
            }
        )


class FlutterwaveWebhookView(APIView):
    """POST /wallet/flw-webhook/ — public endpoint, authenticated by an
    HMAC-SHA256 signature (flutterwave-signature header) over the raw body.

    Deliberately event-name-agnostic: whatever the payload's `reference` is,
    both settle paths are tried and each is a safe no-op if it doesn't match
    a pending transaction of that type. Neither one trusts the webhook body's
    status — both re-fetch the authoritative state from Flutterwave first.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        raw_body = request.body
        signature = request.headers.get('flutterwave-signature')
        if not flutterwave.verify_webhook_signature(raw_body, signature):
            return Response({'detail': 'Invalid signature.'}, status=401)

        data = request.data.get('data') or {}
        reference = data.get('reference') or request.data.get('reference')

        if reference:
            services.handle_deposit_webhook(reference)
            services.handle_transfer_webhook(reference)

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
