"""Public API views for Bitnob virtual cards.

Admin visibility (read-only) lives in adminpanel/views.py, alongside the
rest of the admin panel's endpoints — same split as crypto/kyc.
"""

from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import CardStatus
from .serializers import (
    CardBalanceActionSerializer,
    CardTransactionSerializer,
    RequestCardSerializer,
    TerminateCardSerializer,
    VirtualCardSerializer,
)


def _error_response(exc: services.CardServiceError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


def _get_card_or_404(request):
    card = services.get_card(request.user)
    if card is None:
        return None, Response({'detail': 'No virtual card found.'}, status=404)
    return card, None


class RequestCardView(APIView):
    """POST /api/cards/request/ — { pin, address? }

    KYC- and PIN-gated. Creates the card in Bitnob (async — status comes
    back pending); poll GET /api/cards/status/ for the final state.
    """

    def post(self, request):
        serializer = RequestCardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            card = services.request_card(request.user, pin=data['pin'], address=data.get('address', ''))
        except services.CardServiceError as exc:
            return _error_response(exc)

        return Response(VirtualCardSerializer(card).data, status=201)


class CardStatusView(APIView):
    """GET /api/cards/status/ — current user's card, refreshed from Bitnob
    if still pending/processing. { status: null } if no card exists yet."""

    def get(self, request):
        card = services.get_card(request.user)
        if card is None:
            return Response({'status': None})

        if card.status in (CardStatus.PENDING,) or card.created_status == 'processing':
            card = services.sync_card_status(card)

        return Response(VirtualCardSerializer(card).data)


class FundCardView(APIView):
    """POST /api/cards/fund/ — { ngn_amount, pin }"""

    def post(self, request):
        card, error = _get_card_or_404(request)
        if error:
            return error

        serializer = CardBalanceActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            card_tx = services.fund_card(request.user, card, ngn_amount=data['ngn_amount'], pin=data['pin'])
        except services.CardServiceError as exc:
            return _error_response(exc)

        return Response(CardTransactionSerializer(card_tx).data, status=201)


class WithdrawCardView(APIView):
    """POST /api/cards/withdraw/ — { ngn_amount, pin }"""

    def post(self, request):
        card, error = _get_card_or_404(request)
        if error:
            return error

        serializer = CardBalanceActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            card_tx = services.withdraw_card(request.user, card, ngn_amount=data['ngn_amount'], pin=data['pin'])
        except services.CardServiceError as exc:
            return _error_response(exc)

        return Response(CardTransactionSerializer(card_tx).data, status=201)


class FreezeCardView(APIView):
    """POST /api/cards/freeze/"""

    def post(self, request):
        card, error = _get_card_or_404(request)
        if error:
            return error
        try:
            card = services.freeze_card(card)
        except services.CardServiceError as exc:
            return _error_response(exc)
        return Response(VirtualCardSerializer(card).data)


class UnfreezeCardView(APIView):
    """POST /api/cards/unfreeze/"""

    def post(self, request):
        card, error = _get_card_or_404(request)
        if error:
            return error
        try:
            card = services.unfreeze_card(card)
        except services.CardServiceError as exc:
            return _error_response(exc)
        return Response(VirtualCardSerializer(card).data)


class TerminateCardView(APIView):
    """POST /api/cards/terminate/ — { reason, pin }"""

    def post(self, request):
        card, error = _get_card_or_404(request)
        if error:
            return error

        serializer = TerminateCardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            card = services.terminate_card(request.user, card, reason=data['reason'], pin=data['pin'])
        except services.CardServiceError as exc:
            return _error_response(exc)

        return Response(VirtualCardSerializer(card).data)


class CardTransactionHistoryView(APIView):
    """GET /api/cards/transactions/ — latest 50 for the current user's card."""

    def get(self, request):
        card, error = _get_card_or_404(request)
        if error:
            return error
        return Response(CardTransactionSerializer(services.list_transactions(card), many=True).data)
