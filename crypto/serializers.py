"""DRF serializers for crypto trading (built up in phases)."""

from decimal import Decimal

from rest_framework import serializers

from .models import (
    CryptoDepositAddress,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoQuote,
    CryptoWallet,
    CryptoWithdrawal,
)
from .services import SUPPORTED_COINS


class CryptoFeeSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CryptoFeeSettings
        fields = ['id', 'fee_type', 'flat_usd', 'percent', 'is_active', 'updated_at']
        read_only_fields = ['id', 'fee_type', 'updated_at']


class CryptoFeeSettingsUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CryptoFeeSettings
        fields = ['flat_usd', 'percent', 'is_active']

    def validate_flat_usd(self, value):
        if value < 0:
            raise serializers.ValidationError('Flat fee cannot be negative.')
        return value

    def validate_percent(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError('Percent must be between 0 and 100.')
        return value


class CryptoWalletSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = CryptoWallet
        fields = ['coin', 'name', 'available', 'reserved', 'total', 'updated_at']

    def get_name(self, obj: CryptoWallet) -> str:
        return SUPPORTED_COINS.get(obj.coin, {}).get('name', obj.coin.upper())

    def get_total(self, obj: CryptoWallet) -> str:
        return str(obj.total)


class CryptoQuoteRequestSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=['buy', 'sell', 'swap'])
    coin = serializers.CharField(max_length=10)
    amount = serializers.DecimalField(max_digits=24, decimal_places=8, min_value=Decimal('0'))
    to_coin = serializers.CharField(max_length=10, required=False, allow_blank=True)


class CryptoQuoteSerializer(serializers.ModelSerializer):
    expires_in = serializers.SerializerMethodField()

    class Meta:
        model = CryptoQuote
        fields = [
            'id',
            'quote_type',
            'coin',
            'to_coin',
            'coin_amount',
            'rate_ngn',
            'to_rate_ngn',
            'fee_ngn',
            'total_ngn',
            'to_coin_amount',
            'expires_at',
            'expires_in',
        ]
        read_only_fields = fields

    def get_expires_in(self, obj: CryptoQuote) -> int:
        from django.utils import timezone

        remaining = (obj.expires_at - timezone.now()).total_seconds()
        return max(int(remaining), 0)


class CryptoOrderSerializer(serializers.ModelSerializer):
    payment_proof = serializers.SerializerMethodField()

    class Meta:
        model = CryptoOrder
        fields = [
            'id',
            'reference',
            'order_type',
            'coin',
            'to_coin',
            'coin_amount',
            'to_coin_amount',
            'rate_ngn',
            'to_rate_ngn',
            'fee_ngn',
            'total_ngn',
            'status',
            'payment_proof',
            'note',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_payment_proof(self, obj: CryptoOrder):
        if not obj.payment_proof:
            return None
        request = self.context.get('request')
        url = obj.payment_proof.url
        return request.build_absolute_uri(url) if request else url


class OrderRequestSerializer(serializers.Serializer):
    """Shared shape for placing an order from an already-locked quote —
    used for buy, sell, and swap alike."""

    quote_id = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_blank=True)


class VerifyBuyPaymentSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=64)

    def validate_reference(self, value: str) -> str:
        return value.strip()


class PaymentProofSerializer(serializers.Serializer):
    proof = serializers.ImageField()


class DepositAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = CryptoDepositAddress
        fields = ['coin', 'network', 'address', 'created_at']
        read_only_fields = fields


class CryptoWithdrawalSerializer(serializers.ModelSerializer):
    class Meta:
        model = CryptoWithdrawal
        fields = [
            'id',
            'reference',
            'coin',
            'network',
            'address',
            'amount',
            'fee_coin',
            'net_amount',
            'status',
            'tx_id',
            'note',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class WithdrawRequestSerializer(serializers.Serializer):
    coin = serializers.CharField(max_length=10)
    amount = serializers.DecimalField(max_digits=24, decimal_places=8, min_value=Decimal('0'))
    address = serializers.CharField(max_length=255)
    network = serializers.CharField(max_length=20, required=False, allow_blank=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_blank=True)
