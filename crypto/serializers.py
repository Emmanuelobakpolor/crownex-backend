"""DRF serializers for crypto trading (built up in phases)."""

from decimal import Decimal

from rest_framework import serializers

from .models import (
    CryptoDepositAddress,
    CryptoDepositEvent,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoQuote,
    CryptoWallet,
    CryptoWithdrawal,
    SettlementStatus,
)
from .services import coin_name


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
        return coin_name(obj.coin)

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
            # Swap only: which route executes it, and for instant swaps the
            # gross Quidax quote and our fee in the destination coin
            # (to_coin_amount = gross_to_amount - fee_to_coin, an estimate
            # until Quidax reports the executed amount).
            'swap_method',
            'gross_to_amount',
            'fee_to_coin',
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
    refunded_ngn = serializers.SerializerMethodField()

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
            'swap_method',
            'fee_to_coin',
            'refunded_ngn',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_refunded_ngn(self, obj: CryptoOrder) -> str | None:
        """NGN paid to the user's wallet in place of a swap's destination coin
        (the buy leg failed after the sell leg went through) — the app shows
        this as a refund rather than a plain failure."""
        if obj.order_type != 'swap' or obj.status != 'failed':
            return None
        if not obj.logs.filter(event__in=['swap_refunded_ngn', 'admin_swap_compensated']).exists():
            return None
        from .orders import _swap_net_ngn

        return str(_swap_net_ngn(obj))

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
    pin = serializers.CharField(min_length=4, max_length=4)
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


class CryptoDepositSerializer(serializers.ModelSerializer):
    """One credited deposit plus what happened to it — the app's history
    shows it as held crypto or, with auto-convert, the NGN it became.

    conversion_status: held (auto-convert off), converting, converted,
    skipped (below the minimum sell / no NGN market) or failed."""

    auto_convert = serializers.SerializerMethodField()
    conversion_status = serializers.SerializerMethodField()
    ngn_amount = serializers.SerializerMethodField()
    rate_ngn = serializers.SerializerMethodField()
    order_reference = serializers.SerializerMethodField()

    class Meta:
        model = CryptoDepositEvent
        fields = [
            'id',
            'coin',
            'amount',
            'auto_convert',
            'conversion_status',
            'ngn_amount',
            'rate_ngn',
            'order_reference',
            'created_at',
        ]
        read_only_fields = fields

    @staticmethod
    def _settlement(obj: CryptoDepositEvent):
        try:
            return obj.settlement
        except CryptoDepositEvent.settlement.RelatedObjectDoesNotExist:
            return None  # credited before settlements existed

    def _converted_order(self, obj: CryptoDepositEvent):
        s = self._settlement(obj)
        if s and s.auto_convert and s.status == SettlementStatus.COMPLETED and s.order_id:
            return s.order
        return None

    def get_auto_convert(self, obj: CryptoDepositEvent) -> bool:
        s = self._settlement(obj)
        return bool(s and s.auto_convert)

    def get_conversion_status(self, obj: CryptoDepositEvent) -> str:
        s = self._settlement(obj)
        if not s or not s.auto_convert:
            return 'held'
        if self._converted_order(obj):
            return 'converted'
        if s.status == SettlementStatus.SKIPPED:
            return 'skipped'
        if s.status == SettlementStatus.FAILED:
            return 'failed'
        return 'converting'

    def get_ngn_amount(self, obj: CryptoDepositEvent) -> str | None:
        order = self._converted_order(obj)
        return str(order.total_ngn) if order else None

    def get_rate_ngn(self, obj: CryptoDepositEvent) -> str | None:
        order = self._converted_order(obj)
        return str(order.rate_ngn) if order else None

    def get_order_reference(self, obj: CryptoDepositEvent) -> str | None:
        order = self._converted_order(obj)
        return order.reference if order else None


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
    pin = serializers.CharField(min_length=4, max_length=4)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_blank=True)


class WithdrawEstimateSerializer(serializers.Serializer):
    coin = serializers.CharField(max_length=10)
    amount = serializers.DecimalField(max_digits=24, decimal_places=8, min_value=Decimal('0'))


class CryptoSettingsSerializer(serializers.Serializer):
    auto_convert_deposits = serializers.BooleanField()
