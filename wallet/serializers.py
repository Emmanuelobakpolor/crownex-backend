"""DRF serializers for the NGN wallet API."""

from rest_framework import serializers

from .models import Transaction, Wallet


class WalletBalanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ['ngn_balance']


class InitiateDepositSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    tx_ref = serializers.CharField(max_length=64)
    method = serializers.ChoiceField(choices=['bank_transfer', 'ussd'])
    bank_code = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_tx_ref(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs):
        if attrs['method'] == 'ussd' and not attrs.get('bank_code'):
            raise serializers.ValidationError(
                {'bank_code': 'Select a bank to generate a USSD code.'}
            )
        return attrs


class VerifyPaymentSerializer(serializers.Serializer):
    tx_ref = serializers.CharField(max_length=64)

    def validate_tx_ref(self, value: str) -> str:
        return value.strip()


class WithdrawSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    account_name = serializers.CharField(max_length=150)
    bank_name = serializers.CharField(max_length=100)
    bank_code = serializers.CharField(max_length=20)
    account_number = serializers.CharField(max_length=10)

    def validate_account_number(self, value: str) -> str:
        value = value.strip()
        if len(value) != 10 or not value.isdigit():
            raise serializers.ValidationError('Account number must be exactly 10 digits.')
        return value


class ResolveAccountSerializer(serializers.Serializer):
    account_number = serializers.CharField(max_length=10)
    bank_code = serializers.CharField(max_length=20)

    def validate_account_number(self, value: str) -> str:
        value = value.strip()
        if len(value) != 10 or not value.isdigit():
            raise serializers.ValidationError('Account number must be exactly 10 digits.')
        return value


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            'id',
            'tx_type',
            'amount',
            'status',
            'reference',
            'deposit_method',
            'bank_name',
            'account_number',
            'account_name',
            'note',
            'created_at',
        ]
        read_only_fields = fields
