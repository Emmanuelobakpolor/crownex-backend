"""Serializers for the CrownEx admin panel API."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.models import User


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
