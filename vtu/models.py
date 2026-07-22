"""Airtime + data purchases (VTU), fulfilled through PluginNG."""

import secrets
import time
import uuid

from django.conf import settings
from django.db import models


def generate_vtu_reference() -> str:
    """`VTU-<ms epoch>-<6 hex chars>` — sent to PluginNG as custom_reference."""
    return f'VTU-{int(time.time() * 1000)}-{secrets.token_hex(3)}'


class ServiceType(models.TextChoices):
    AIRTIME = 'airtime', 'Airtime'
    DATA = 'data', 'Data'


class VTUStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    SUCCESS = 'success', 'Success'
    FAILED = 'failed', 'Failed'
    REVERSED = 'reversed', 'Reversed'


# PluginNG's numeric transaction status codes (see their API docs / webhook payload).
PLUGINNG_STATUS_MAP = {
    '0': VTUStatus.PENDING,
    '1': VTUStatus.SUCCESS,
    '4': VTUStatus.FAILED,
    '2': VTUStatus.REVERSED,
}


class VTUTransaction(models.Model):
    """A single airtime or data purchase, tracked through its PluginNG lifecycle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='vtu_transactions',
    )

    service = models.CharField(max_length=16, choices=ServiceType.choices)
    network = models.CharField(max_length=30)
    subcategory_id = models.CharField(max_length=10)
    plan_id = models.CharField(max_length=100, blank=True)  # data plan label; blank for airtime
    phone = models.CharField(max_length=15)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16,
        choices=VTUStatus.choices,
        default=VTUStatus.PENDING,
    )

    # Our reference, sent to PluginNG as custom_reference — this is what ties
    # their requery/webhook responses back to this row.
    reference = models.CharField(max_length=64, unique=True)
    provider_ref = models.CharField(max_length=64, blank=True)  # PluginNG's own "ref"
    provider_response = models.TextField(blank=True)
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
        return f'{self.service} ₦{self.amount} ({self.status}) — {self.user.email}'
