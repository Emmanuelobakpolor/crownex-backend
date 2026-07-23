"""API views for VTU purchases: catalogue, airtime, data, status, and history."""

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import (
    BuyAirtimeSerializer,
    BuyCableSerializer,
    BuyDataSerializer,
    BuyElectricitySerializer,
    VerifyMeterSerializer,
    VerifySmartcardSerializer,
    VTUTransactionSerializer,
)


def _error_response(exc: services.VTUServiceError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


class CatalogueView(APIView):
    """GET /vtu/catalogue/ — airtime networks, and data networks + their plans."""

    def get(self, request):
        try:
            catalogue = services.get_catalogue()
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(catalogue)


class BuyAirtimeView(APIView):
    """POST /vtu/airtime/"""

    def post(self, request):
        serializer = BuyAirtimeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.buy_airtime(request.user, **serializer.validated_data)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(VTUTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class BuyDataView(APIView):
    """POST /vtu/data/"""

    def post(self, request):
        serializer = BuyDataSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.buy_data(request.user, **serializer.validated_data)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(VTUTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class CableBouquetsView(APIView):
    """GET /vtu/cable/bouquets/?network=gotv"""

    def get(self, request):
        network = request.query_params.get('network', '')
        try:
            bouquets = services.get_cable_bouquets(network)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response({'bouquets': bouquets})


class VerifySmartcardView(APIView):
    """POST /vtu/cable/verify/"""

    def post(self, request):
        serializer = VerifySmartcardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = services.verify_smartcard(**serializer.validated_data)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(data)


class BuyCableView(APIView):
    """POST /vtu/cable/"""

    def post(self, request):
        serializer = BuyCableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.buy_cable(request.user, **serializer.validated_data)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(VTUTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class ElectricityVariationsView(APIView):
    """GET /vtu/electricity/variations/?network=Eko-Electric"""

    def get(self, request):
        network = request.query_params.get('network', '')
        try:
            variations = services.get_electricity_variations(network)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response({'variations': variations})


class VerifyMeterView(APIView):
    """POST /vtu/electricity/verify/"""

    def post(self, request):
        serializer = VerifyMeterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = services.verify_meter(**serializer.validated_data)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(data)


class BuyElectricityView(APIView):
    """POST /vtu/electricity/"""

    def post(self, request):
        serializer = BuyElectricitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx = services.buy_electricity(request.user, **serializer.validated_data)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(VTUTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class TransactionStatusView(APIView):
    """GET /vtu/transactions/<reference>/status/ — requery a pending purchase."""

    def get(self, request, reference):
        try:
            tx = services.requery_transaction(request.user, reference)
        except services.VTUServiceError as exc:
            return _error_response(exc)
        return Response(VTUTransactionSerializer(tx).data)


class TransactionHistoryView(APIView):
    """GET /vtu/transactions/ — latest 50 for the current user."""

    def get(self, request):
        transactions = services.list_transactions(request.user)
        return Response(VTUTransactionSerializer(transactions, many=True).data)


class PluginNGWebhookView(APIView):
    """POST /vtu/webhook/<secret>/

    PluginNG doesn't document a signature header for its webhook, so the
    webhook URL itself — configured once in the PluginNG dashboard's profile
    section — carries a secret path segment instead.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request, secret):
        if secret != settings.PLUGINNG_WEBHOOK_SECRET:
            return Response({'detail': 'Invalid signature.'}, status=401)
        services.handle_webhook(request.data)
        return Response({'status': 'ok'})
