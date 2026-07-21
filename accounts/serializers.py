"""DRF serializers for CrownEx authentication and profile APIs."""

import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import User
from .services import normalize_phone


class UserSerializer(serializers.ModelSerializer):
    """Public user profile payload returned by auth and profile endpoints."""

    profile_picture = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source='date_joined', read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'phone',
            'full_name',
            'profile_picture',
            'is_verified',
            'is_profile_complete',
            'has_transaction_pin',
            'created_at',
        ]
        read_only_fields = fields

    def get_profile_picture(self, obj: User):
        if not obj.profile_picture:
            return None
        request = self.context.get('request')
        url = obj.profile_picture.url
        if request is not None:
            return request.build_absolute_uri(url)
        return url


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()

    def validate_phone(self, value: str) -> str:
        phone = normalize_phone(value)
        if len(phone) < 10 or len(phone) > 15:
            raise serializers.ValidationError(
                'Enter a valid phone number (10–15 digits).'
            )
        if not phone.isdigit():
            raise serializers.ValidationError('Phone number must contain only digits.')
        return phone


class VerifyOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField(min_length=4, max_length=6)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()

    def validate_otp(self, value: str) -> str:
        value = value.strip()
        if not value.isdigit():
            raise serializers.ValidationError('OTP must be numeric.')
        return value


class ResendOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        return value.lower().strip()


class CompleteProfileSerializer(serializers.Serializer):
    email = serializers.EmailField()
    full_name = serializers.CharField(min_length=2, max_length=150)
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, min_length=8)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()

    def validate_full_name(self, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError('Name is too short.')
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


class SetTransactionPinSerializer(serializers.Serializer):
    pin = serializers.CharField(min_length=4, max_length=4)
    pin_confirm = serializers.CharField(min_length=4, max_length=4)

    def validate_pin(self, value: str) -> str:
        if not re.fullmatch(r'\d{4}', value):
            raise serializers.ValidationError('PIN must be exactly 4 digits.')
        return value

    def validate(self, attrs):
        if attrs['pin'] != attrs['pin_confirm']:
            raise serializers.ValidationError(
                {'pin_confirm': 'PINs do not match.'}
            )
        return attrs


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        return value.lower().strip()


class VerifyResetTokenSerializer(serializers.Serializer):
    email = serializers.EmailField()
    token = serializers.CharField(min_length=4, max_length=128)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()

    def validate_token(self, value: str) -> str:
        return value.strip()


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    token = serializers.CharField(min_length=4, max_length=128)
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, min_length=8)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()

    def validate_token(self, value: str) -> str:
        return value.strip()

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


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True, min_length=8)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError(
                {'new_password_confirm': 'Passwords do not match.'}
            )
        try:
            validate_password(attrs['new_password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)}) from exc
        return attrs


class ChangeTransactionPinSerializer(serializers.Serializer):
    current_pin = serializers.CharField(min_length=4, max_length=4)
    new_pin = serializers.CharField(min_length=4, max_length=4)
    new_pin_confirm = serializers.CharField(min_length=4, max_length=4)

    def validate_new_pin(self, value: str) -> str:
        if not re.fullmatch(r'\d{4}', value):
            raise serializers.ValidationError('PIN must be exactly 4 digits.')
        return value

    def validate(self, attrs):
        if attrs['new_pin'] != attrs['new_pin_confirm']:
            raise serializers.ValidationError(
                {'new_pin_confirm': 'PINs do not match.'}
            )
        return attrs


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['full_name', 'phone', 'profile_picture']

    def validate_full_name(self, value: str) -> str:
        value = value.strip()
        if value and len(value) < 2:
            raise serializers.ValidationError('Name is too short.')
        return value

    def validate_phone(self, value: str) -> str:
        if not value:
            return value
        phone = normalize_phone(value)
        if len(phone) < 10 or len(phone) > 15:
            raise serializers.ValidationError(
                'Enter a valid phone number (10–15 digits).'
            )
        qs = User.objects.filter(phone=phone).exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                'This phone number is already in use.'
            )
        return phone
