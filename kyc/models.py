"""Data models for identity verification (KYC) — gates virtual card access.

One KycVerification per user (OneToOneField, same pattern as
QuidaxSubAccount in crypto/models.py). See kyc/services.py for how
match_score maps to status, and kyc/dojah.py for the Dojah API client.
"""

from django.conf import settings
from django.db import models


class KycStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    APPROVED = 'approved', 'Approved'
    REJECTED = 'rejected', 'Rejected'
    REVIEW = 'review', 'Needs review'


class KycVerification(models.Model):
    """A user's identity verification attempt and its outcome.

    selfie/id_document are stored on our own storage (Cloudinary in
    production, per STORAGES in settings.py) rather than relied on from
    Dojah's response — Dojah's own file links expire after about an hour.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='kyc'
    )

    id_type = models.CharField(max_length=20)  # 'nin' | 'bvn'
    id_number = models.CharField(max_length=32)
    full_name = models.CharField(max_length=150, blank=True)
    date_of_birth = models.CharField(max_length=10, blank=True)
    address = models.TextField(blank=True)

    selfie = models.ImageField(upload_to='kyc_selfies/', null=True, blank=True)
    id_document = models.ImageField(upload_to='kyc_documents/', null=True, blank=True)

    dojah_reference_id = models.CharField(max_length=64, blank=True)
    match_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=KycStatus.choices, default=KycStatus.PENDING)
    raw_response = models.JSONField(default=dict, blank=True)
    reviewer_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status'])]
        verbose_name = 'KYC verification'
        verbose_name_plural = 'KYC verifications'

    def __str__(self):
        return f'{self.id_type} verification ({self.status}) — {self.user.email}'


class KycLog(models.Model):
    """Immutable event stream per verification — same purpose as
    CryptoOrderLog: the audit trail admins read to understand exactly what
    happened without guessing from status alone."""

    verification = models.ForeignKey(
        KycVerification, on_delete=models.CASCADE, related_name='logs'
    )
    event = models.CharField(max_length=64)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.event} — {self.verification.user.email}'
