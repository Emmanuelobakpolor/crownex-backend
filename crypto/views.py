"""Public API views for crypto trading (built up in phases).

Admin-only fee management lives in adminpanel/views.py, alongside the rest
of the admin panel's endpoints — this module only exposes what the app
(and the client's fee estimate display) can see without a staff account.
"""

import hashlib
import hmac

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import deposits, orders, services, withdrawals
from .models import OrderStatus
from .serializers import (
    CryptoOrderSerializer,
    CryptoQuoteRequestSerializer,
    CryptoQuoteSerializer,
    CryptoWalletSerializer,
    CryptoWithdrawalSerializer,
    DepositAddressSerializer,
    OrderRequestSerializer,
    PaymentProofSerializer,
    VerifyBuyPaymentSerializer,
    WithdrawEstimateSerializer,
    WithdrawRequestSerializer,
)


def _error_response(exc: services.CryptoServiceError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


class PricesView(APIView):
    """GET /api/crypto/prices/ — public, live NGN prices for supported coins."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        try:
            prices = services.get_prices()
        except services.CryptoServiceError as exc:
            return _error_response(exc)
        return Response({'coins': prices})


class FeesView(APIView):
    """GET /api/crypto/fees/ — public fee config, for client-side estimates only.

    The real charge is whatever gets frozen onto a CryptoQuote at quote
    time — this is display-only and can drift a few seconds behind an
    admin's latest change.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({'fees': services.get_public_fees()})


class WalletsView(APIView):
    """GET /api/crypto/wallets/ — the user's internal balances (source of truth,
    never Quidax's raw wallet balances)."""

    def get(self, request):
        wallets = services.list_wallets(request.user)
        return Response(CryptoWalletSerializer(wallets, many=True).data)


class QuoteView(APIView):
    """POST /api/crypto/quote/ — lock rate + fee for 30 seconds.

    body: { type: buy|sell|swap, coin, amount, to_coin? (swap only) }
    """

    def post(self, request):
        serializer = CryptoQuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            quote = services.create_quote(
                request.user,
                quote_type=data['type'],
                coin=data['coin'],
                amount=data['amount'],
                to_coin=data.get('to_coin'),
            )
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(CryptoQuoteSerializer(quote).data, status=status.HTTP_201_CREATED)


class BuyOrderView(APIView):
    """POST /api/crypto/orders/buy/ — { quote_id, idempotency_key? }

    Response always includes the order; if needs_payment is true, the
    client still owes money (Flutterwave checkout or bank transfer) before
    the crypto is delivered — wallet-funded orders complete synchronously.
    """

    def post(self, request):
        serializer = OrderRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = orders.place_buy_order(
                request.user,
                quote_id=data['quote_id'],
                pin=data['pin'],
                idempotency_key=data.get('idempotency_key') or None,
            )
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        payload = CryptoOrderSerializer(order, context={'request': request}).data
        payload['needs_payment'] = order.status == OrderStatus.PENDING_PAYMENT

        if order.status == OrderStatus.PENDING_PAYMENT:
            if settings.FLW_SECRET_KEY:
                payload['payment_method'] = 'flutterwave'
                payload['flutterwave'] = {
                    'public_key': settings.FLW_PUBLIC_KEY,
                    'tx_ref': order.reference,
                    'amount': str(order.total_ngn),
                    'customer': {
                        'email': request.user.email,
                        'phone': request.user.phone or '',
                        'name': request.user.full_name or request.user.email,
                    },
                }
            else:
                payload['payment_method'] = 'bank_transfer'
                payload['bank_details'] = orders.get_bank_details()

        return Response(payload, status=status.HTTP_201_CREATED)


class VerifyBuyPaymentView(APIView):
    """POST /api/crypto/orders/buy/verify/ — { reference } after Flutterwave checkout."""

    def post(self, request):
        serializer = VerifyBuyPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            order = orders.verify_buy_payment(request.user, serializer.validated_data['reference'])
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(CryptoOrderSerializer(order, context={'request': request}).data)


class PaymentProofUploadView(APIView):
    """POST /api/crypto/orders/<reference>/proof/ — multipart proof for
    bank-transfer buys. Doesn't execute the order itself; an admin reviews
    and approves (see adminpanel in a later phase)."""

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reference):
        serializer = PaymentProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            order = orders.submit_payment_proof(
                request.user, reference, serializer.validated_data['proof']
            )
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(CryptoOrderSerializer(order, context={'request': request}).data)


class SellOrderView(APIView):
    """POST /api/crypto/orders/sell/ — { quote_id, idempotency_key? }

    Sells immediately if the user has enough crypto balance. Otherwise the
    order comes back as waiting_deposit — retry it once funded via
    POST /api/crypto/orders/sell/<reference>/retry/.
    """

    def post(self, request):
        serializer = OrderRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = orders.place_sell_order(
                request.user,
                quote_id=data['quote_id'],
                pin=data['pin'],
                idempotency_key=data.get('idempotency_key') or None,
            )
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        payload = CryptoOrderSerializer(order, context={'request': request}).data
        payload['waiting_deposit'] = order.status == OrderStatus.WAITING_DEPOSIT
        return Response(payload, status=status.HTTP_201_CREATED)


class RetrySellOrderView(APIView):
    """POST /api/crypto/orders/sell/<reference>/retry/ — re-attempt a
    waiting_deposit sell once the user believes they've funded it."""

    def post(self, request, reference):
        try:
            order = orders.retry_sell_after_deposit(request.user, reference)
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(CryptoOrderSerializer(order, context={'request': request}).data)


class SwapOrderView(APIView):
    """POST /api/crypto/orders/swap/ — { quote_id, idempotency_key? }

    Requires the full source-coin balance up front — fails immediately
    (no waiting_deposit fallback) if there isn't enough.
    """

    def post(self, request):
        serializer = OrderRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = orders.place_swap_order(
                request.user,
                quote_id=data['quote_id'],
                pin=data['pin'],
                idempotency_key=data.get('idempotency_key') or None,
            )
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(
            CryptoOrderSerializer(order, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class OrderHistoryView(APIView):
    """GET /api/crypto/orders/ — latest 50 for the current user."""

    def get(self, request):
        history = orders.list_orders(request.user)
        return Response(CryptoOrderSerializer(history, many=True, context={'request': request}).data)


class DepositAddressView(APIView):
    """GET /api/crypto/address/<coin>/?network=TRC20

    Provisions a Quidax sub-account on first use if the user doesn't have
    one yet — see crypto/deposits.py for why that isn't done at signup.
    """

    def get(self, request, coin):
        network = request.query_params.get('network')
        try:
            address = deposits.get_deposit_address(request.user, coin, network=network)
        except services.CryptoServiceError as exc:
            return _error_response(exc)
        return Response(DepositAddressSerializer(address).data)


class EnsureAccountView(APIView):
    """POST /api/crypto/account/ensure/ — best-effort Quidax sub-account
    warm-up. Meant to be called the instant a user opens the crypto
    section of the app (fire-and-forget, no need to await it) so the
    sub-account already exists by the time they actually request a
    deposit address. Always answers 200 — a failure here is logged
    server-side and retried by the normal lazy path, not something the
    UI needs to handle as an error.
    """

    def post(self, request):
        try:
            deposits.get_or_create_sub_account(request.user)
            return Response({'ready': True})
        except services.CryptoServiceError:
            return Response({'ready': False})


class QuidaxWebhookView(APIView):
    """POST /api/crypto/webhook/quidax/ — HMAC-SHA512(raw_body, secret)
    verified against X-Quidax-Signature. Always answers 200 for events we
    don't (yet) handle, so Quidax doesn't retry forever on something that
    was never going to be processed."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        secret = settings.QUIDAX_WEBHOOK_SECRET
        signature = request.headers.get('X-Quidax-Signature', '')
        if not secret:
            return Response({'detail': 'Webhook not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        expected = hmac.new(secret.encode('utf-8'), request.body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return Response({'detail': 'Invalid signature.'}, status=status.HTTP_401_UNAUTHORIZED)

        event = request.data.get('event')
        if event == 'deposit.successful':
            deposits.handle_deposit_webhook(request.data)
        elif event == 'wallet.address.generated':
            deposits.handle_address_generated_webhook(request.data)
        elif event in ('order.done', 'order.completed'):
            deposits.handle_order_webhook(request.data)
        elif event == 'withdraw.successful':
            withdrawals.handle_withdraw_webhook(request.data, rejected=False)
        elif event == 'withdraw.rejected':
            withdrawals.handle_withdraw_webhook(request.data, rejected=True)

        return Response({'status': 'ok'})


class WithdrawView(APIView):
    """POST /api/crypto/withdraw/ — { coin, amount, address, network?, idempotency_key? }

    No admin approval — address format, min notional, daily NGN cap, and
    balance reservation are the guards that make this safe unattended.
    """

    def post(self, request):
        serializer = WithdrawRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            withdrawal = withdrawals.request_withdrawal(
                request.user,
                coin=data['coin'],
                amount=data['amount'],
                address=data['address'],
                pin=data['pin'],
                network=data.get('network') or None,
                idempotency_key=data.get('idempotency_key') or None,
            )
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(CryptoWithdrawalSerializer(withdrawal).data, status=status.HTTP_201_CREATED)


class WithdrawEstimateView(APIView):
    """GET /api/crypto/withdraw/estimate/?coin=&amount= — fee/net-amount
    preview with no side effects, for showing an accurate summary before
    the user confirms a withdrawal."""

    def get(self, request):
        serializer = WithdrawEstimateSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            estimate = withdrawals.estimate_withdrawal(coin=data['coin'], amount=data['amount'])
        except services.CryptoServiceError as exc:
            return _error_response(exc)

        return Response(estimate)


class WithdrawalHistoryView(APIView):
    """GET /api/crypto/withdrawals/ — latest 50 for the current user."""

    def get(self, request):
        history = withdrawals.list_withdrawals(request.user)
        return Response(CryptoWithdrawalSerializer(history, many=True).data)
