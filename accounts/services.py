"""Business logic for registration, OTP, login gates, and password reset."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import send_otp_email
from .models import OTPPurpose, User, VerificationOTP
from .tokens import generate_otp_code, hash_code, otp_expiry, verify_code


class AuthServiceError(Exception):
    """Domain error with machine-readable code and optional HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def normalize_phone(phone: str) -> str:
    """Normalize Nigerian-style numbers to a consistent digits form."""
    digits = ''.join(ch for ch in phone if ch.isdigit())
    if digits.startswith('234') and len(digits) >= 13:
        digits = '0' + digits[3:]
    return digits


def issue_tokens(user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }


def _invalidate_active_otps(user: User, purpose: str) -> None:
    VerificationOTP.objects.filter(
        user=user,
        purpose=purpose,
        is_used=False,
    ).update(is_used=True)


def create_and_send_otp(user: User, purpose: str = OTPPurpose.REGISTRATION) -> str:
    """Create a hashed OTP, invalidate previous ones, and email the plain code."""
    cooldown = getattr(settings, 'OTP_RESEND_COOLDOWN_SECONDS', 60)
    latest = (
        VerificationOTP.objects.filter(user=user, purpose=purpose)
        .order_by('-created_at')
        .first()
    )
    if latest and not latest.is_used:
        elapsed = (timezone.now() - latest.created_at).total_seconds()
        if elapsed < cooldown:
            wait = int(cooldown - elapsed)
            raise AuthServiceError(
                f'Please wait {wait} seconds before requesting another code.',
                code='otp_cooldown',
                status=429,
            )

    _invalidate_active_otps(user, purpose)
    plain = generate_otp_code()
    VerificationOTP.objects.create(
        user=user,
        code_hash=hash_code(plain),
        purpose=purpose,
        expires_at=otp_expiry(),
    )
    send_otp_email(user.email, plain, purpose=purpose)
    return plain


@transaction.atomic
def register_user(email: str, phone: str) -> User:
    """Step 1: create pending user and send registration OTP."""
    email = email.lower().strip()
    phone = normalize_phone(phone)

    if User.objects.filter(email__iexact=email, is_profile_complete=True).exists():
        raise AuthServiceError(
            'An account with this email already exists.',
            code='email_exists',
        )

    existing_phone = User.objects.filter(phone=phone).exclude(email__iexact=email).first()
    if existing_phone and existing_phone.is_profile_complete:
        raise AuthServiceError(
            'An account with this phone number already exists.',
            code='phone_exists',
        )

    user = User.objects.filter(email__iexact=email).first()
    if user:
        if user.is_profile_complete and user.is_verified:
            raise AuthServiceError(
                'An account with this email already exists.',
                code='email_exists',
            )
        # Restart incomplete registration
        user.phone = phone
        user.is_verified = False
        user.is_profile_complete = False
        user.has_transaction_pin = False
        user.transaction_pin_hash = ''
        user.full_name = ''
        user.set_unusable_password()
        user.save()
    else:
        # Free phone if held by another incomplete registration
        User.objects.filter(phone=phone, is_profile_complete=False).exclude(
            email__iexact=email
        ).update(phone=None)
        user = User.objects.create_user(
            email=email,
            phone=phone,
            is_verified=False,
            is_profile_complete=False,
        )

    create_and_send_otp(user, purpose=OTPPurpose.REGISTRATION)
    return user


def verify_registration_otp(email: str, otp: str) -> User:
    """Step 2: verify 4-digit OTP."""
    email = email.lower().strip()
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist as exc:
        raise AuthServiceError(
            'No registration found for this email.',
            code='user_not_found',
            status=404,
        ) from exc

    if user.is_verified:
        return user

    otp_obj = (
        VerificationOTP.objects.filter(
            user=user,
            purpose=OTPPurpose.REGISTRATION,
            is_used=False,
        )
        .order_by('-created_at')
        .first()
    )
    if not otp_obj:
        raise AuthServiceError(
            'No active verification code. Please request a new one.',
            code='otp_missing',
        )

    max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 5)
    if otp_obj.attempts >= max_attempts:
        otp_obj.is_used = True
        otp_obj.save(update_fields=['is_used'])
        raise AuthServiceError(
            'Too many invalid attempts. Please request a new code.',
            code='otp_locked',
        )

    if otp_obj.is_expired:
        otp_obj.is_used = True
        otp_obj.save(update_fields=['is_used'])
        raise AuthServiceError(
            'Verification code has expired. Please request a new one.',
            code='otp_expired',
        )

    if not verify_code(otp, otp_obj.code_hash):
        otp_obj.attempts += 1
        otp_obj.save(update_fields=['attempts'])
        remaining = max_attempts - otp_obj.attempts
        raise AuthServiceError(
            f'Invalid verification code. {remaining} attempt(s) remaining.',
            code='otp_invalid',
        )

    otp_obj.is_used = True
    otp_obj.save(update_fields=['is_used'])
    user.is_verified = True
    user.save(update_fields=['is_verified', 'updated_at'])
    return user


def resend_registration_otp(email: str) -> None:
    email = email.lower().strip()
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist as exc:
        raise AuthServiceError(
            'No registration found for this email.',
            code='user_not_found',
            status=404,
        ) from exc

    if user.is_verified and user.is_profile_complete:
        raise AuthServiceError(
            'This account is already verified.',
            code='already_verified',
        )

    # Allow resend even if verified but profile incomplete (edge case)
    if user.is_verified and not user.is_profile_complete:
        # Still allow resend only if not verified for login — treat as already verified
        raise AuthServiceError(
            'Email already verified. Continue setting up your profile.',
            code='already_verified',
        )

    create_and_send_otp(user, purpose=OTPPurpose.REGISTRATION)


@transaction.atomic
def complete_profile(
    email: str,
    full_name: str,
    password: str,
) -> tuple[User, dict]:
    """Step 3: set full name + password; issue JWT for PIN step."""
    email = email.lower().strip()
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist as exc:
        raise AuthServiceError(
            'No registration found for this email.',
            code='user_not_found',
            status=404,
        ) from exc

    if not user.is_verified:
        raise AuthServiceError(
            'Please verify your email with the OTP before continuing.',
            code='email_not_verified',
            status=403,
        )

    user.full_name = full_name.strip()
    user.set_password(password)
    user.is_profile_complete = True
    user.save()
    tokens = issue_tokens(user)
    return user, tokens


@transaction.atomic
def set_transaction_pin(user: User, pin: str) -> User:
    """Step 4: store hashed 4-digit transaction PIN."""
    if not user.is_verified or not user.is_profile_complete:
        raise AuthServiceError(
            'Complete registration before setting a transaction PIN.',
            code='registration_incomplete',
            status=403,
        )
    user.set_transaction_pin(pin)
    user.save(
        update_fields=['transaction_pin_hash', 'has_transaction_pin', 'updated_at']
    )
    return user


def login_user(email: str, password: str) -> tuple[User, dict]:
    email = email.lower().strip()
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist as exc:
        raise AuthServiceError(
            'Invalid email or password.',
            code='invalid_credentials',
            status=401,
        ) from exc

    if not user.is_active:
        raise AuthServiceError(
            'This account has been deactivated.',
            code='account_disabled',
            status=403,
        )

    if not user.is_verified:
        raise AuthServiceError(
            'Email is not verified. Please verify your account or resend the code.',
            code='email_not_verified',
            status=403,
        )

    if not user.is_profile_complete or not user.has_usable_password():
        raise AuthServiceError(
            'Please complete your profile setup before signing in.',
            code='profile_incomplete',
            status=403,
        )

    authenticated = authenticate(username=email, password=password)
    if authenticated is None:
        # authenticate may fail if backend expects username field named email
        if not user.check_password(password):
            raise AuthServiceError(
                'Invalid email or password.',
                code='invalid_credentials',
                status=401,
            )
        authenticated = user

    tokens = issue_tokens(authenticated)
    return authenticated, tokens


def request_password_reset(email: str) -> None:
    """Always succeed outwardly; only send OTP if user exists."""
    email = email.lower().strip()
    user = User.objects.filter(email__iexact=email).first()
    if not user or not user.is_profile_complete:
        return
    try:
        create_and_send_otp(user, purpose=OTPPurpose.PASSWORD_RESET)
    except AuthServiceError:
        # Cooldown: re-raise so client can back off
        raise


def verify_reset_otp(email: str, otp: str) -> User:
    email = email.lower().strip()
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist as exc:
        raise AuthServiceError(
            'Invalid or expired reset code.',
            code='reset_invalid',
        ) from exc

    otp_obj = (
        VerificationOTP.objects.filter(
            user=user,
            purpose=OTPPurpose.PASSWORD_RESET,
            is_used=False,
        )
        .order_by('-created_at')
        .first()
    )
    if not otp_obj or otp_obj.is_expired:
        raise AuthServiceError(
            'Invalid or expired reset code.',
            code='reset_invalid',
        )

    max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 5)
    if otp_obj.attempts >= max_attempts:
        otp_obj.is_used = True
        otp_obj.save(update_fields=['is_used'])
        raise AuthServiceError(
            'Too many invalid attempts. Please request a new code.',
            code='otp_locked',
        )

    if not verify_code(otp, otp_obj.code_hash):
        otp_obj.attempts += 1
        otp_obj.save(update_fields=['attempts'])
        raise AuthServiceError(
            'Invalid or expired reset code.',
            code='reset_invalid',
        )

    return user


@transaction.atomic
def reset_password(email: str, otp: str, password: str) -> User:
    user = verify_reset_otp(email, otp)

    otp_obj = (
        VerificationOTP.objects.filter(
            user=user,
            purpose=OTPPurpose.PASSWORD_RESET,
            is_used=False,
        )
        .order_by('-created_at')
        .first()
    )
    if otp_obj:
        otp_obj.is_used = True
        otp_obj.save(update_fields=['is_used'])

    user.set_password(password)
    user.save(update_fields=['password', 'updated_at'])
    return user


def change_password(user: User, current_password: str, new_password: str) -> User:
    if not user.check_password(current_password):
        raise AuthServiceError(
            'Current password is incorrect.',
            code='invalid_password',
            status=400,
        )
    user.set_password(new_password)
    user.save(update_fields=['password', 'updated_at'])
    return user


def change_transaction_pin(user: User, current_pin: str, new_pin: str) -> User:
    if not user.has_transaction_pin or not user.check_transaction_pin(current_pin):
        raise AuthServiceError(
            'Current PIN is incorrect.',
            code='invalid_pin',
            status=400,
        )
    user.set_transaction_pin(new_pin)
    user.save(
        update_fields=['transaction_pin_hash', 'has_transaction_pin', 'updated_at']
    )
    return user


def logout_user(refresh_token: str) -> None:
    try:
        token = RefreshToken(refresh_token)
        token.blacklist()
    except Exception as exc:
        raise AuthServiceError(
            'Invalid or expired refresh token.',
            code='invalid_token',
            status=400,
        ) from exc
