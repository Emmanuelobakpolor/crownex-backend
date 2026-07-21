"""OTP generation and hashing helpers."""

import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone


def generate_otp_code(length: int | None = None) -> str:
    """Generate a cryptographically random numeric OTP."""
    length = length or getattr(settings, 'OTP_LENGTH', 4)
    return ''.join(secrets.choice(string.digits) for _ in range(length))


def hash_code(code: str) -> str:
    return make_password(code)


def verify_code(code: str, code_hash: str) -> bool:
    return check_password(code, code_hash)


def otp_expiry() -> timezone.datetime:
    minutes = getattr(settings, 'OTP_EXPIRY_MINUTES', 10)
    return timezone.now() + timedelta(minutes=minutes)


def generate_reset_token() -> str:
    """Secure random token for password-reset links (fallback to OTP for mobile)."""
    return secrets.token_urlsafe(32)
