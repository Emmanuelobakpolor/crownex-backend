"""API views for gift card purchases: rate, brands, products, buy, history."""

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import GiftCardBuySerializer, GiftCardPurchaseSerializer


def _error_response(exc: services.GiftCardServiceError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


class RateView(APIView):
    """GET /giftcards/rate/"""

    def get(self, request):
        return Response({'ngn_per_usd': str(services.get_rate())})


class BrandsView(APIView):
    """GET /giftcards/brands/?country_code=US"""

    def get(self, request):
        country_code = request.query_params.get('country_code', 'US')
        try:
            brands = services.get_brands(country_code)
        except services.GiftCardServiceError as exc:
            return _error_response(exc)
        return Response(brands)


class ProductsView(APIView):
    """GET /giftcards/products/?brand=Apple&country_code=US"""

    def get(self, request):
        brand = request.query_params.get('brand', '')
        country_code = request.query_params.get('country_code', 'US')
        try:
            products = services.get_products(brand, country_code)
        except services.GiftCardServiceError as exc:
            return _error_response(exc)
        return Response(products)


class BuyView(APIView):
    """POST /giftcards/buy/"""

    def post(self, request):
        serializer = GiftCardBuySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            purchase = services.buy_gift_card(
                request.user,
                product_id=data['product_id'],
                unit_price_usd=data['unit_price_usd'],
                brand=data['brand'],
                country_code=data['country_code'],
                transaction_pin=data['pin'],
            )
        except services.GiftCardServiceError as exc:
            return _error_response(exc)
        return Response(GiftCardPurchaseSerializer(purchase).data, status=status.HTTP_201_CREATED)


class HistoryView(APIView):
    """GET /giftcards/history/ — latest 50 for the current user."""

    def get(self, request):
        purchases = services.list_purchases(request.user)
        return Response(GiftCardPurchaseSerializer(purchases, many=True).data)


class WebhookView(APIView):
    """POST /giftcards/webhook/<secret>/

    Reloadly doesn't sign this with a header scheme pinned down here, so —
    same approach as the PluginNG webhook — the secret lives in the
    webhook URL itself, configured once in the Reloadly dashboard.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request, secret):
        if secret != settings.RELOADLY_WEBHOOK_SECRET:
            return Response({'detail': 'Invalid signature.'}, status=401)
        services.handle_webhook(request.data)
        return Response({'status': 'ok'})
