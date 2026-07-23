"""DRF serializers for gift card purchases."""

from rest_framework import serializers

from .models import GiftCardPurchase


class GiftCardBuySerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    unit_price_usd = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    brand = serializers.CharField(max_length=120)
    brand_asset = serializers.CharField(max_length=255, required=False, allow_blank=True)
    country_code = serializers.CharField(max_length=4)
    pin = serializers.CharField(max_length=4, min_length=4)


class GiftCardPurchaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = GiftCardPurchase
        fields = [
            'id',
            'brand',
            'country_code',
            'product_id',
            'product_name',
            'unit_price_usd',
            'rate_ngn',
            'amount_ngn',
            'reference',
            'status',
            'redeem_code',
            'redeem_pin',
            'note',
            'created_at',
        ]
        read_only_fields = fields
