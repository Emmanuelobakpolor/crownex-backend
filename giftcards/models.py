"""Digital gift card purchases (Apple/iTunes, Amazon, Google Play, etc.),
fulfilled through Reloadly."""

import uuid

from django.conf import settings
from django.db import models


def generate_giftcard_reference() -> str:
    """`GCBUY-<uuid>` — sent to Reloadly as customIdentifier."""
    return f'GCBUY-{uuid.uuid4()}'


class GiftCardStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class GiftCardPurchase(models.Model):
    """A single gift card purchase, tracked through its Reloadly order."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='giftcard_purchases',
    )

    brand = models.CharField(max_length=120)
    country_code = models.CharField(max_length=4)
    product_id = models.BigIntegerField()
    product_name = models.CharField(max_length=160, blank=True)

    unit_price_usd = models.DecimalField(max_digits=12, decimal_places=2)
    rate_ngn = models.DecimalField(max_digits=12, decimal_places=2)
    amount_ngn = models.DecimalField(max_digits=12, decimal_places=2)

    # Our reference, sent to Reloadly as customIdentifier.
    reference = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=GiftCardStatus.choices,
        default=GiftCardStatus.PENDING,
    )

    reloadly_tx_id = models.CharField(max_length=32, blank=True)
    redeem_code = models.CharField(max_length=120, blank=True)
    redeem_pin = models.CharField(max_length=60, blank=True)
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
        return f'{self.brand} ${self.unit_price_usd} ({self.status}) — {self.user.email}'
