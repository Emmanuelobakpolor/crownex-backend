"""NGN wallet + transaction ledger backed by Flutterwave."""

import secrets
import time
import uuid

from django.conf import settings
from django.db import models


class Wallet(models.Model):
    """1:1 NGN balance per user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wallet',
    )
    ngn_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.email} — ₦{self.ngn_balance}'


def generate_withdrawal_reference() -> str:
    """`WD-<ms epoch>-<6 hex chars>` — unique enough without a DB round trip."""
    return f'WD-{int(time.time() * 1000)}-{secrets.token_hex(3)}'


class TransactionType(models.TextChoices):
    DEPOSIT = 'deposit', 'Deposit'
    WITHDRAWAL = 'withdrawal', 'Withdrawal'


class TransactionStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class Transaction(models.Model):
    """A single deposit or withdrawal, tracked through its Flutterwave lifecycle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='transactions',
    )
    tx_type = models.CharField(max_length=16, choices=TransactionType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(
        max_length=16,
        choices=TransactionStatus.choices,
        default=TransactionStatus.PENDING,
    )

    # Unique client reference for deposits (Flutterwave tx_ref), auto-generated
    # for withdrawals (Flutterwave transfer reference).
    reference = models.CharField(max_length=64, unique=True)
    flw_tx_ref = models.CharField(max_length=64, unique=True, null=True, blank=True)

    # Withdrawal destination — blank for deposits.
    bank_name = models.CharField(max_length=100, blank=True)
    bank_code = models.CharField(max_length=20, blank=True)
    account_number = models.CharField(max_length=10, blank=True)
    account_name = models.CharField(max_length=150, blank=True)

    note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.tx_type} ₦{self.amount} ({self.status}) — {self.user.email}'
