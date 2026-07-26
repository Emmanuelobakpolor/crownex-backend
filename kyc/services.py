"""Business logic for identity verification (KYC).

submit_verification is the only entry point that talks to Dojah — it
saves the submitted data, calls the matching Dojah lookup, and decides
status from the returned match confidence:
  >= 90  -> approved  (Dojah's own match:true threshold)
  70-89  -> review    (human admin decides — see admin_approve/admin_reject)
  < 70   -> rejected

admin_approve/admin_reject are the manual override path for rows sitting
in `review`, mirroring crypto/orders.py's admin_* functions.
"""

from __future__ import annotations

import base64
from decimal import Decimal

from django.db import transaction

from . import dojah
from .dojah import DojahError
from .models import KycLog, KycStatus, KycVerification

APPROVE_THRESHOLD = Decimal('90')
REVIEW_THRESHOLD = Decimal('70')

_VERIFY_FUNCS = {
    'nin': dojah.verify_nin_with_selfie,
    'bvn': dojah.verify_bvn_with_selfie,
}


class KycServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _log(verification: KycVerification, event: str, detail: str = '') -> None:
    KycLog.objects.create(verification=verification, event=event, detail=detail)


def _status_from_score(score: Decimal | None) -> str:
    if score is None:
        return KycStatus.REVIEW
    if score >= APPROVE_THRESHOLD:
        return KycStatus.APPROVED
    if score >= REVIEW_THRESHOLD:
        return KycStatus.REVIEW
    return KycStatus.REJECTED


def get_verification(user) -> KycVerification | None:
    return KycVerification.objects.filter(user=user).first()


@transaction.atomic
def submit_verification(
    user,
    *,
    id_type: str,
    id_number: str,
    selfie_file,
    id_document_file=None,
    address: str = '',
) -> KycVerification:
    if id_type not in _VERIFY_FUNCS:
        raise KycServiceError(f'Unsupported id_type: {id_type}.', code='unsupported_id_type')

    verification, _ = KycVerification.objects.update_or_create(
        user=user,
        defaults={
            'id_type': id_type,
            'id_number': id_number,
            'address': address,
            'selfie': selfie_file,
            'id_document': id_document_file,
            'status': KycStatus.PENDING,
        },
    )
    _log(verification, 'submitted', f'id_type={id_type}')

    selfie_file.seek(0)
    selfie_base64 = base64.b64encode(selfie_file.read()).decode('ascii')

    try:
        payload = _VERIFY_FUNCS[id_type](**{id_type: id_number}, selfie_base64=selfie_base64)
    except DojahError as exc:
        _log(verification, 'dojah_error', exc.message)
        raise KycServiceError(
            f'Could not verify identity: {exc.message}', code='dojah_unreachable', status=502
        )

    entity = payload.get('entity') or {}
    selfie_check = entity.get('selfie_verification') or payload.get('selfie_verification') or {}
    confidence = selfie_check.get('confidence_value')
    score = Decimal(str(confidence)) if confidence is not None else None

    verification.dojah_reference_id = str(payload.get('reference_id') or '')
    verification.match_score = score
    verification.full_name = entity.get('full_name') or entity.get('first_name', '')
    verification.date_of_birth = entity.get('date_of_birth', '')
    verification.raw_response = payload
    verification.status = _status_from_score(score)
    verification.save(
        update_fields=[
            'dojah_reference_id',
            'match_score',
            'full_name',
            'date_of_birth',
            'raw_response',
            'status',
            'updated_at',
        ]
    )

    _log(
        verification,
        'dojah_verified',
        f'match_score={score} status={verification.status}',
    )
    return verification


def admin_approve(verification: KycVerification, note: str = '') -> KycVerification:
    if verification.status not in (KycStatus.REVIEW, KycStatus.PENDING, KycStatus.REJECTED):
        raise KycServiceError('This verification cannot be approved.', code='invalid_state')

    verification.status = KycStatus.APPROVED
    verification.reviewer_note = note
    verification.save(update_fields=['status', 'reviewer_note', 'updated_at'])
    _log(verification, 'admin_approved', note or 'Approved by admin.')
    return verification


def admin_reject(verification: KycVerification, note: str = '') -> KycVerification:
    if verification.status not in (KycStatus.REVIEW, KycStatus.PENDING, KycStatus.APPROVED):
        raise KycServiceError('This verification cannot be rejected.', code='invalid_state')

    verification.status = KycStatus.REJECTED
    verification.reviewer_note = note
    verification.save(update_fields=['status', 'reviewer_note', 'updated_at'])
    _log(verification, 'admin_rejected', note or 'Rejected by admin.')
    return verification
