"""Email delivery helpers for OTP and password reset."""

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_otp_email(email: str, code: str, purpose: str = 'registration') -> None:
    """Send a verification OTP to the user.

    In development EMAIL_BACKEND is console, so the code prints to the server log.
    Structure is ready for SMS later (same code payload).
    """
    if purpose == 'password_reset':
        subject = 'CrownEx password reset code'
        body = (
            f'Your CrownEx password reset code is: {code}\n\n'
            f'This code expires in {getattr(settings, "OTP_EXPIRY_MINUTES", 10)} minutes.\n'
            'If you did not request this, you can ignore this email.'
        )
    else:
        subject = 'CrownEx verification code'
        body = (
            f'Your CrownEx verification code is: {code}\n\n'
            f'This code expires in {getattr(settings, "OTP_EXPIRY_MINUTES", 10)} minutes.\n'
            'If you did not create an account, you can ignore this email.'
        )

    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@crownex.app')
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        logger.exception('Failed to send OTP email to %s', email)
        raise
