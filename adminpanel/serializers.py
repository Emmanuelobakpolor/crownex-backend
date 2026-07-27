"""Serializers for the CrownEx admin panel API."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.models import User
from cards.models import CardLog, CardTransaction, VirtualCard
from cards.services import micro_to_decimal
from crypto.models import CryptoOrder, CryptoOrderLog, CryptoWithdrawal, CryptoWithdrawalLog
from kyc.models import KycLog, KycVerification


class CreateAdminSerializer(serializers.Serializer):
    """Bootstraps an admin/staff account — used by the Postman-only create endpoint."""

    email = serializers.EmailField()
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, min_length=8)

    def validate_email(self, value: str) -> str:
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError(
                {'password_confirm': 'Passwords do not match.'}
            )
        try:
            validate_password(attrs['password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)}) from exc
        return attrs


class AdminLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()


class AdminProfileUpdateSerializer(serializers.Serializer):
    """Admin's own account update — email and/or name change, password-confirmed."""

    new_email = serializers.EmailField(required=False)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    current_password = serializers.CharField(write_only=True)

    def validate_new_email(self, value: str) -> str:
        value = value.lower().strip()
        user = self.context['user']
        if User.objects.filter(email__iexact=value).exclude(pk=user.pk).exists():
            raise serializers.ValidationError('This email is already in use.')
        return value

    def validate_current_password(self, value: str) -> str:
        user = self.context['user']
        if not user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate(self, attrs):
        if not attrs.get('new_email') and 'full_name' not in attrs:
            raise serializers.ValidationError('Provide new_email and/or full_name to update.')
        return attrs


class AdminCryptoOrderLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CryptoOrderLog
        fields = ['event', 'detail', 'created_at']
        read_only_fields = fields


class AdminCryptoOrderSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    payment_proof = serializers.SerializerMethodField()
    logs = AdminCryptoOrderLogSerializer(many=True, read_only=True)

    class Meta:
        model = CryptoOrder
        fields = [
            'id',
            'reference',
            'user_email',
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
            'quidax_order_id',
            'payment_proof',
            'note',
            'created_at',
            'updated_at',
            'logs',
        ]
        read_only_fields = fields

    def get_payment_proof(self, obj: CryptoOrder):
        if not obj.payment_proof:
            return None
        request = self.context.get('request')
        url = obj.payment_proof.url
        return request.build_absolute_uri(url) if request else url


class AdminCryptoOrderActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['approve', 'reject', 'confirm_deposit', 'retry'])
    note = serializers.CharField(required=False, allow_blank=True, default='')


class AdminCryptoWithdrawalLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CryptoWithdrawalLog
        fields = ['event', 'detail', 'created_at']
        read_only_fields = fields


class AdminCryptoWithdrawalSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    logs = AdminCryptoWithdrawalLogSerializer(many=True, read_only=True)

    class Meta:
        model = CryptoWithdrawal
        fields = [
            'id',
            'reference',
            'user_email',
            'coin',
            'network',
            'address',
            'amount',
            'fee_coin',
            'net_amount',
            'status',
            'quidax_withdrawal_id',
            'tx_id',
            'note',
            'created_at',
            'updated_at',
            'logs',
        ]
        read_only_fields = fields


class AdminCryptoWithdrawalActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['complete', 'reject'])
    tx_id = serializers.CharField(required=False, allow_blank=True, default='')
    note = serializers.CharField(required=False, allow_blank=True, default='')


class AdminKycLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = KycLog
        fields = ['event', 'detail', 'created_at']
        read_only_fields = fields


class AdminKycVerificationSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    selfie = serializers.SerializerMethodField()
    id_document = serializers.SerializerMethodField()
    logs = AdminKycLogSerializer(many=True, read_only=True)

    class Meta:
        model = KycVerification
        fields = [
            'id',
            'user_email',
            'id_type',
            'id_number',
            'full_name',
            'date_of_birth',
            'address',
            'selfie',
            'id_document',
            'dojah_reference_id',
            'match_score',
            'status',
            'reviewer_note',
            'created_at',
            'updated_at',
            'logs',
        ]
        read_only_fields = fields

    def get_selfie(self, obj: KycVerification):
        if not obj.selfie:
            return None
        request = self.context.get('request')
        url = obj.selfie.url
        return request.build_absolute_uri(url) if request else url

    def get_id_document(self, obj: KycVerification):
        if not obj.id_document:
            return None
        request = self.context.get('request')
        url = obj.id_document.url
        return request.build_absolute_uri(url) if request else url


class AdminKycActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['approve', 'reject'])
    note = serializers.CharField(required=False, allow_blank=True, default='')


class AdminCardLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CardLog
        fields = ['event', 'detail', 'created_at']
        read_only_fields = fields


class AdminCardTransactionSerializer(serializers.ModelSerializer):
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


class AdminVirtualCardSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    balance_display = serializers.SerializerMethodField()
    logs = AdminCardLogSerializer(many=True, read_only=True)
    transactions = AdminCardTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = VirtualCard
        fields = [
            'id',
            'user_email',
            'status',
            'created_status',
            'card_brand',
            'masked_pan',
            'cardholder_name',
            'balance_display',
            'created_at',
            'updated_at',
            'logs',
            'transactions',
        ]
        read_only_fields = fields

    def get_balance_display(self, obj: VirtualCard) -> str:
        return str(micro_to_decimal(obj.balance_usd_micro))
