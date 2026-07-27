"""Business logic for Bitnob-issued virtual cards.

request_card is gated on KYC approval (kyc.services.get_verification) and
the user's transaction PIN (same 4-digit PIN crypto/gift cards already
use — no separate card PIN concept, per product decision). fund_card
follows crypto/orders.py's discipline for money that leaves the building:
debit the internal NGN wallet first, refund only if the external call
demonstrably failed before anything moved on Bitnob's side.
"""

from __future__ import annotations

import secrets
import time
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction

from kyc import services as kyc_services
from kyc.models import KycStatus
from wallet.services import WalletServiceError, credit_wallet, debit_wallet

from . import bitnob
from .bitnob import BitnobError
from .models import (
    CardLog,
    CardStatus,
    CardTransaction,
    CardTransactionStatus,
    CardTransactionType,
    VirtualCard,
)

MICRO_PER_UNIT = Decimal('1000000')


class CardServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def micro_to_decimal(value: str | int) -> Decimal:
    """'5000000' -> Decimal('5.00'). The one place this conversion lives."""
    return (Decimal(str(value)) / MICRO_PER_UNIT).quantize(Decimal('0.01'))


def decimal_to_micro(value: Decimal) -> int:
    """Decimal('5.00') -> 5000000."""
    return int((value * MICRO_PER_UNIT).to_integral_value(rounding=ROUND_DOWN))


def _check_transaction_pin(user, pin: str) -> None:
    if not user.has_transaction_pin:
        raise CardServiceError('Set a transaction PIN before requesting a card.', code='pin_not_set')
    if not user.check_transaction_pin(pin):
        raise CardServiceError('Incorrect transaction PIN.', code='invalid_pin', status=401)


def _log(card: VirtualCard, event: str, detail: str = '') -> None:
    CardLog.objects.create(card=card, event=event, detail=detail)


def _card_reference() -> str:
    return f'CARD-{int(time.time() * 1000)}-{secrets.token_hex(3)}'


def _transaction_reference(prefix: str) -> str:
    return f'{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(3)}'


def get_card(user) -> VirtualCard | None:
    return VirtualCard.objects.filter(user=user).first()


def _local_phone_number(phone: str) -> str:
    """User.phone is stored normalized as local digits (e.g. '08012345678',
    see accounts.services.normalize_phone) — Bitnob wants the number
    without the leading trunk '0' or country code (e.g. '8012345678')."""
    digits = phone or ''
    if digits.startswith('0'):
        digits = digits[1:]
    return digits


def _customer_payload(user, verification, address: str) -> dict:
    """Builds Bitnob's nested `customer` object from data already collected
    during KYC — no separate re-entry of identity fields."""
    id_type_map = {'nin': 'national_id', 'bvn': 'national_id'}
    first_name, _, last_name = (verification.full_name or user.full_name).partition(' ')
    return {
        'customer_type': 'individual',
        'first_name': first_name or user.full_name or 'Unknown',
        'last_name': last_name or '',
        'email': user.email,
        'phone_number': _local_phone_number(user.phone),
        'dial_code': '+234',
        'date_of_birth': verification.date_of_birth or '1990-01-01',
        'id_type': id_type_map.get(verification.id_type, 'national_id'),
        'id_number': verification.id_number,
        'line1': address or 'Not provided',
        'city': 'Lagos',
        'state': 'Lagos',
        'postal_code': '100001',
        'country': 'NGA',
    }


@transaction.atomic
def request_card(user, *, pin: str, address: str = '') -> VirtualCard:
    _check_transaction_pin(user, pin)

    if VirtualCard.objects.filter(user=user).exists():
        raise CardServiceError('You already have a virtual card.', code='card_exists')

    verification = kyc_services.get_verification(user)
    if verification is None or verification.status != KycStatus.APPROVED:
        raise CardServiceError(
            'Complete identity verification before requesting a card.', code='kyc_not_approved'
        )

    reference = _card_reference()
    card = VirtualCard.objects.create(user=user, reference=reference, status=CardStatus.PENDING)

    try:
        payload = bitnob.create_card(
            amount_micro=0,
            currency='USD',
            name=verification.full_name or user.full_name or user.email,
            reference=reference,
            customer=_customer_payload(user, verification, address),
        )
    except BitnobError as exc:
        _log(card, 'bitnob_error', exc.message)
        raise CardServiceError(f'Could not create card: {exc.message}', code='bitnob_unreachable', status=502)

    data = (payload.get('data') or {}).get('card') or {}
    card.bitnob_card_id = data.get('id', '')
    card.bitnob_customer_id = data.get('customer_id', '')
    card.status = data.get('status', CardStatus.PENDING)
    card.created_status = data.get('created_status', '')
    card.card_brand = data.get('card_brand', '')
    card.masked_pan = data.get('masked_pan', '')
    card.cardholder_name = data.get('name', '')
    card.balance_usd_micro = str(data.get('balance_amount', '0'))
    card.raw_response = payload
    card.save()

    _log(card, 'card_requested', f'bitnob_card_id={card.bitnob_card_id}')
    return card


def sync_card_status(card: VirtualCard) -> VirtualCard:
    """Polls Bitnob for the latest provisioning/status state — used by
    GET /api/cards/status/ instead of a webhook receiver in this pass."""
    if not card.bitnob_card_id:
        return card

    try:
        payload = bitnob.get_card(card.bitnob_card_id)
    except BitnobError as exc:
        _log(card, 'bitnob_error', f'sync failed: {exc.message}')
        return card

    data = (payload.get('data') or {}).get('card') or {}
    if not data:
        return card

    card.status = data.get('status', card.status)
    card.created_status = data.get('created_status', card.created_status)
    card.masked_pan = data.get('masked_pan', card.masked_pan)
    card.card_brand = data.get('card_brand', card.card_brand)
    card.balance_usd_micro = str(data.get('balance_amount', card.balance_usd_micro))
    card.raw_response = payload
    card.save()
    return card


def _adjust_card_balance(user, card: VirtualCard, *, ngn_amount: Decimal, type_: str) -> CardTransaction:
    if card.status != CardStatus.ACTIVE:
        raise CardServiceError('Card is not active.', code='card_not_active')

    usd_amount = ngn_amount / settings.BITNOB_NGN_PER_USD
    amount_micro = decimal_to_micro(usd_amount)
    if amount_micro <= 0:
        raise CardServiceError('Amount too small.', code='amount_too_low')

    reference = _transaction_reference('FUND' if type_ == 'fund' else 'WD')

    if type_ == 'fund':
        try:
            debit_wallet(user, ngn_amount)
        except WalletServiceError as exc:
            raise CardServiceError(exc.message, code=exc.code, status=exc.status)

    card_tx = CardTransaction.objects.create(
        card=card,
        reference=reference,
        type=CardTransactionType.FUNDING if type_ == 'fund' else CardTransactionType.WITHDRAWAL,
        amount_usd_micro=str(amount_micro),
        ngn_amount=ngn_amount,
        description=f'{type_} via Crownex wallet',
    )

    try:
        payload = bitnob.fund_or_withdraw(
            card.bitnob_card_id, amount_micro=amount_micro, type_=type_, reference=reference
        )
    except BitnobError as exc:
        card_tx.status = CardTransactionStatus.FAILED
        card_tx.save(update_fields=['status', 'updated_at'])
        if type_ == 'fund':
            credit_wallet(user, ngn_amount)
            _log(card, 'fund_failed_refunded', f'{exc.message} — ₦{ngn_amount} refunded to wallet.')
        else:
            _log(card, 'withdraw_failed', exc.message)
        raise CardServiceError(f'Card {type_} failed: {exc.message}', code='bitnob_unreachable', status=502)

    tx_data = (payload.get('data') or {}).get('transaction') or {}
    card_tx.bitnob_transaction_id = tx_data.get('id', '')
    card_tx.fee_usd_micro = str(tx_data.get('fee_amount', ''))
    card_tx.status = {
        'pending': CardTransactionStatus.PENDING,
        'completed': CardTransactionStatus.COMPLETED,
        'failed': CardTransactionStatus.FAILED,
    }.get(tx_data.get('status', 'pending'), CardTransactionStatus.PENDING)
    card_tx.save()

    _log(card, f'{type_}_requested', f'₦{ngn_amount} -> {amount_micro} micro-units')
    return card_tx


def fund_card(user, card: VirtualCard, *, ngn_amount: Decimal, pin: str) -> CardTransaction:
    _check_transaction_pin(user, pin)
    return _adjust_card_balance(user, card, ngn_amount=ngn_amount, type_='fund')


def withdraw_card(user, card: VirtualCard, *, ngn_amount: Decimal, pin: str) -> CardTransaction:
    _check_transaction_pin(user, pin)
    return _adjust_card_balance(user, card, ngn_amount=ngn_amount, type_='withdraw')


def freeze_card(card: VirtualCard) -> VirtualCard:
    try:
        bitnob.update_status(card.bitnob_card_id, 'frozen')
    except BitnobError as exc:
        raise CardServiceError(f'Could not freeze card: {exc.message}', code='bitnob_unreachable', status=502)
    card.status = CardStatus.FROZEN
    card.save(update_fields=['status', 'updated_at'])
    _log(card, 'frozen', 'Frozen by user.')
    return card


def unfreeze_card(card: VirtualCard) -> VirtualCard:
    try:
        bitnob.update_status(card.bitnob_card_id, 'active')
    except BitnobError as exc:
        raise CardServiceError(f'Could not unfreeze card: {exc.message}', code='bitnob_unreachable', status=502)
    card.status = CardStatus.ACTIVE
    card.save(update_fields=['status', 'updated_at'])
    _log(card, 'unfrozen', 'Unfrozen by user.')
    return card


def terminate_card(user, card: VirtualCard, *, reason: str, pin: str) -> VirtualCard:
    _check_transaction_pin(user, pin)
    try:
        bitnob.terminate_card(card.bitnob_card_id, reason)
    except BitnobError as exc:
        if exc.error_code == 'CARD_TERMINATION_COOLDOWN':
            raise CardServiceError(
                'This card was created less than 24 hours ago. Freeze it instead '
                'until the cooldown period has passed.',
                code='termination_cooldown',
            )
        raise CardServiceError(f'Could not terminate card: {exc.message}', code='bitnob_unreachable', status=502)

    card.status = CardStatus.TERMINATED
    card.save(update_fields=['status', 'updated_at'])
    _log(card, 'terminated', reason)
    return card


def list_transactions(card: VirtualCard):
    return card.transactions.all()[:50]
