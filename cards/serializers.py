"""DRF serializers for Bitnob virtual cards."""

from decimal import Decimal

from rest_framework import serializers

from .models import CardTransaction, VirtualCard
from .services import micro_to_decimal


class VirtualCardSerializer(serializers.ModelSerializer):
    balance_display = serializers.SerializerMethodField()

    class Meta:
        model = VirtualCard
        fields = [
            'status',
            'created_status',
            'card_brand',
            'masked_pan',
            'cardholder_name',
            'balance_display',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_balance_display(self, obj: VirtualCard) -> str:
        return str(micro_to_decimal(obj.balance_usd_micro))


class CardTransactionSerializer(serializers.ModelSerializer):
    amount_display = serializers.SerializerMethodField()

    class Meta:
        model = CardTransaction
        fields = [
            'reference',
            'type',
            'status',
            'amount_display',
            'ngn_amount',
            'description',
            'created_at',
        ]
        read_only_fields = fields

    def get_amount_display(self, obj: CardTransaction) -> str:
        return str(micro_to_decimal(obj.amount_usd_micro))


class RequestCardSerializer(serializers.Serializer):
    pin = serializers.CharField(min_length=4, max_length=4)
    address = serializers.CharField(required=False, allow_blank=True, default='')


class CardBalanceActionSerializer(serializers.Serializer):
    ngn_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    pin = serializers.CharField(min_length=4, max_length=4)


class TerminateCardSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)
    pin = serializers.CharField(min_length=4, max_length=4)
