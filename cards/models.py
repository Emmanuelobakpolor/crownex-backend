"""Data models for Bitnob-issued USD virtual cards.

One VirtualCard per user (OneToOneField, same pattern as
QuidaxSubAccount/KycVerification) — this app only supports a single card
per user for now, matching the Flutter dashboard's single-card UI.

Amounts from Bitnob are stored as the raw micro-unit strings it returns
(1,000,000 = 1 whole currency unit) rather than converted to Decimal at
write time — see cards/services.py:micro_to_decimal for the one place
that conversion happens, to avoid float/precision drift across writes.
"""

from django.conf import settings
from django.db import models


class CardStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    ACTIVE = 'active', 'Active'
    FROZEN = 'frozen', 'Frozen'
    TERMINATED = 'terminated', 'Terminated'


class VirtualCard(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='virtual_card'
    )

    bitnob_card_id = models.CharField(max_length=64, unique=True, blank=True)
    bitnob_customer_id = models.CharField(max_length=64, blank=True)
    reference = models.CharField(max_length=64, unique=True)

    status = models.CharField(max_length=12, choices=CardStatus.choices, default=CardStatus.PENDING)
    created_status = models.CharField(max_length=16, blank=True)
    card_brand = models.CharField(max_length=20, blank=True)
    masked_pan = models.CharField(max_length=24, blank=True)
    cardholder_name = models.CharField(max_length=150, blank=True)

    balance_usd_micro = models.CharField(max_length=32, default='0')
    raw_response = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status'])]

    def __str__(self):
        return f'{self.masked_pan or "unprovisioned"} ({self.status}) — {self.user.email}'


class CardTransactionType(models.TextChoices):
    FUNDING = 'funding', 'Funding'
    WITHDRAWAL = 'withdrawal', 'Withdrawal'


class CardTransactionStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class CardTransaction(models.Model):
    """One row per fund/withdraw request — mirrors Bitnob's own transaction
    shape (type, status, amount, fee) plus the NGN side of the conversion,
    which Bitnob has no notion of."""

    card = models.ForeignKey(VirtualCard, on_delete=models.CASCADE, related_name='transactions')
    bitnob_transaction_id = models.CharField(max_length=64, blank=True)
    reference = models.CharField(max_length=64, unique=True)

    type = models.CharField(max_length=12, choices=CardTransactionType.choices)
    status = models.CharField(
        max_length=12, choices=CardTransactionStatus.choices, default=CardTransactionStatus.PENDING
    )

    amount_usd_micro = models.CharField(max_length=32)
    fee_usd_micro = models.CharField(max_length=32, blank=True)
    ngn_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['card', '-created_at'])]

    def __str__(self):
        return f'{self.type} {self.amount_usd_micro} ({self.status}) — {self.card.user.email}'


class CardLog(models.Model):
    """Immutable event stream per card — same audit-trail purpose as
    CryptoOrderLog/KycLog."""

    card = models.ForeignKey(VirtualCard, on_delete=models.CASCADE, related_name='logs')
    event = models.CharField(max_length=64)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.event} — {self.card.user.email}'
