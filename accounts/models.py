from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Manager for email-based User model."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('is_verified', True)
        extra_fields.setdefault('is_profile_complete', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        if not password:
            raise ValueError('Superuser must have a password.')

        return self.create_user(email, password=password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Custom user authenticated by email (matches CrownEx mobile sign-in)."""

    email = models.EmailField(unique=True, db_index=True)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=150, blank=True)
    profile_picture = models.ImageField(
        upload_to='profiles/',
        null=True,
        blank=True,
    )

    is_verified = models.BooleanField(
        default=False,
        help_text='True after OTP verification during registration.',
    )
    is_profile_complete = models.BooleanField(
        default=False,
        help_text='True after full name and password are set.',
    )
    has_transaction_pin = models.BooleanField(default=False)
    transaction_pin_hash = models.CharField(max_length=128, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ['-date_joined']
        verbose_name = 'user'
        verbose_name_plural = 'users'

    def __str__(self):
        return self.email

    def set_transaction_pin(self, pin: str) -> None:
        self.transaction_pin_hash = make_password(pin)
        self.has_transaction_pin = True

    def check_transaction_pin(self, pin: str) -> bool:
        if not self.transaction_pin_hash:
            return False
        return check_password(pin, self.transaction_pin_hash)

    @property
    def can_login(self) -> bool:
        return (
            self.is_active
            and self.is_verified
            and self.is_profile_complete
            and self.has_usable_password()
        )


class OTPPurpose(models.TextChoices):
    REGISTRATION = 'registration', 'Registration'
    PASSWORD_RESET = 'password_reset', 'Password Reset'


class VerificationOTP(models.Model):
    """Hashed one-time codes for registration and password reset."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='otps',
    )
    code_hash = models.CharField(max_length=128)
    purpose = models.CharField(
        max_length=32,
        choices=OTPPurpose.choices,
        default=OTPPurpose.REGISTRATION,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'purpose', 'is_used']),
        ]

    def __str__(self):
        return f'{self.purpose} OTP for {self.user.email}'

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired
