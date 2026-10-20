"""Business logic for the NGN wallet: deposits, withdrawals, and webhooks.

Every balance mutation goes through `select_for_update()` inside an atomic
block, and every credit/refund path checks the transaction's current status
before acting — that's what makes verify-payment, the webhook, and a retried
client call all safe to run more than once for the same reference.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import flutterwave
from .flutterwave import FlutterwaveError
from .models import (
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
    generate_withdrawal_reference,
)

logger = logging.getLogger(__name__)

MIN_DEPOSIT = Decimal('100')
MIN_WITHDRAWAL = Decimal('500')


class WalletServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def get_or_create_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


@transaction.atomic
def debit_wallet(user, amount: Decimal) -> Wallet:
    """Generic balance debit for other apps (e.g. vtu). Raises on insufficient funds."""
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    if wallet.ngn_balance < amount:
        raise WalletServiceError('Insufficient balance.', code='insufficient_balance')
    wallet.ngn_balance = wallet.ngn_balance - amount
    wallet.save(update_fields=['ngn_balance', 'updated_at'])
    return wallet


@transaction.atomic
def credit_wallet(user, amount: Decimal) -> Wallet:
    """Generic balance credit for other apps (e.g. vtu refunds)."""
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    wallet.ngn_balance = wallet.ngn_balance + amount
    wallet.save(update_fields=['ngn_balance', 'updated_at'])
    return wallet


# ─── Deposit ──────────────────────────────────────────────────────────────


@transaction.atomic
def initiate_deposit(user, amount: Decimal, tx_ref: str) -> Transaction:
    if amount < MIN_DEPOSIT:
        raise WalletServiceError(
            f'Minimum deposit amount is ₦{MIN_DEPOSIT}.', code='amount_too_low'
        )

    if Transaction.objects.filter(flw_tx_ref=tx_ref).exists():
        raise WalletServiceError(
            'This transaction reference has already been used.',
            code='duplicate_tx_ref',
            status=409,
        )

    get_or_create_wallet(user)

    try:
        return Transaction.objects.create(
            user=user,
            tx_type=TransactionType.DEPOSIT,
            amount=amount,
            status=TransactionStatus.PENDING,
            reference=tx_ref,
            flw_tx_ref=tx_ref,
        )
    except IntegrityError as exc:
        raise WalletServiceError(
            'This transaction reference has already been used.',
            code='duplicate_tx_ref',
            status=409,
        ) from exc


def _credit_wallet_for_deposit(tx: Transaction) -> Wallet:
    """Idempotent credit — only runs if the transaction isn't already completed."""
    with transaction.atomic():
        locked_tx = Transaction.objects.select_for_update().get(pk=tx.pk)
        if locked_tx.status == TransactionStatus.COMPLETED:
            return get_or_create_wallet(locked_tx.user)

        wallet = Wallet.objects.select_for_update().get(user=locked_tx.user)
        wallet.ngn_balance = wallet.ngn_balance + locked_tx.amount
        wallet.save(update_fields=['ngn_balance', 'updated_at'])

        locked_tx.status = TransactionStatus.COMPLETED
        locked_tx.save(update_fields=['status', 'updated_at'])
        return wallet


def verify_payment(user, tx_ref: str) -> tuple[Wallet, Transaction]:
    try:
        tx = Transaction.objects.get(
            flw_tx_ref=tx_ref, user=user, tx_type=TransactionType.DEPOSIT
        )
    except Transaction.DoesNotExist as exc:
        raise WalletServiceError(
            'No deposit found for this reference.', code='tx_not_found', status=404
        ) from exc

    if tx.status == TransactionStatus.COMPLETED:
        return get_or_create_wallet(user), tx

    try:
        payload = flutterwave.verify_transaction(tx_ref)
    except FlutterwaveError as exc:
        raise WalletServiceError(
            f'Could not verify payment: {exc.message}', code='flw_unreachable', status=502
        ) from exc

    data = payload.get('data') or {}
    flw_ok = payload.get('status') == 'success' and data.get('status') == 'successful'
    paid_amount = Decimal(str(data.get('amount', 0)))

    if not flw_ok or paid_amount < tx.amount:
        tx.status = TransactionStatus.FAILED
        tx.note = payload.get('message', 'Verification failed.')
        tx.save(update_fields=['status', 'note', 'updated_at'])
        raise WalletServiceError(
            'Payment could not be verified.', code='verification_failed'
        )

    wallet = _credit_wallet_for_deposit(tx)
    tx.refresh_from_db()
    return wallet, tx


def handle_deposit_webhook(flw_tx_ref: str) -> None:
    """charge.completed — credit exactly once, silently no-op on unknown refs."""
    tx = Transaction.objects.filter(
        flw_tx_ref=flw_tx_ref, tx_type=TransactionType.DEPOSIT
    ).first()
    if tx is None or tx.status == TransactionStatus.COMPLETED:
        return
    _credit_wallet_for_deposit(tx)


# ─── Withdrawal ───────────────────────────────────────────────────────────


@transaction.atomic
def _debit_and_create_withdrawal(
    user, amount: Decimal, account_name: str, bank_name: str, bank_code: str,
    account_number: str,
) -> Transaction:
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    if wallet.ngn_balance < amount:
        raise WalletServiceError('Insufficient balance.', code='insufficient_balance')

    wallet.ngn_balance = wallet.ngn_balance - amount
    wallet.save(update_fields=['ngn_balance', 'updated_at'])

    for _ in range(5):
        reference = generate_withdrawal_reference()
        try:
            return Transaction.objects.create(
                user=user,
                tx_type=TransactionType.WITHDRAWAL,
                amount=amount,
                status=TransactionStatus.PENDING,
                reference=reference,
                account_name=account_name,
                bank_name=bank_name,
                bank_code=bank_code,
                account_number=account_number,
            )
        except IntegrityError:
            continue
    raise WalletServiceError('Could not generate a withdrawal reference.', status=500)


def _refund_and_fail(tx_id, note: str) -> None:
    with transaction.atomic():
        tx = Transaction.objects.select_for_update().get(pk=tx_id)
        if tx.status != TransactionStatus.PENDING:
            return
        wallet = Wallet.objects.select_for_update().get(user=tx.user)
        wallet.ngn_balance = wallet.ngn_balance + tx.amount
        wallet.save(update_fields=['ngn_balance', 'updated_at'])

        tx.status = TransactionStatus.FAILED
        tx.note = note
        tx.save(update_fields=['status', 'note', 'updated_at'])


def request_withdrawal(
    user,
    *,
    amount: Decimal,
    account_name: str,
    bank_name: str,
    bank_code: str,
    account_number: str,
) -> Transaction:
    if amount < MIN_WITHDRAWAL:
        raise WalletServiceError(
            f'Minimum withdrawal amount is ₦{MIN_WITHDRAWAL}.', code='amount_too_low'
        )
    if len(account_number) != 10 or not account_number.isdigit():
        raise WalletServiceError(
            'Account number must be exactly 10 digits.', code='invalid_account_number'
        )

    tx = _debit_and_create_withdrawal(
        user, amount, account_name, bank_name, bank_code, account_number
    )

    try:
        payload = flutterwave.initiate_transfer(
            account_bank=bank_code,
            account_number=account_number,
            amount=float(amount),
            narration=f'[{getattr(settings, "APP_NAME", "CrownEx")}] Withdrawal',
            reference=tx.reference,
            beneficiary_name=account_name,
        )
    except FlutterwaveError as exc:
        logger.error(
            'Flutterwave transfer failed for %s (bank=%s): %s | payload=%s',
            tx.reference, bank_code, exc.message, getattr(exc, 'payload', None),
        )
        _refund_and_fail(tx.pk, f'Transfer request failed: {exc.message}')
        raise WalletServiceError(
            'Withdrawal could not be initiated. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        ) from exc

    if payload.get('status') != 'success':
        note = payload.get('message', 'Transfer rejected by Flutterwave.')
        logger.error('Flutterwave rejected transfer %s (bank=%s): %s', tx.reference, bank_code, payload)
        _refund_and_fail(tx.pk, note)
        raise WalletServiceError(
            'Withdrawal could not be initiated. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        )

    transfer_id = str((payload.get('data') or {}).get('id') or '')
    if transfer_id:
        Transaction.objects.filter(pk=tx.pk).update(flw_transfer_id=transfer_id)
        tx.flw_transfer_id = transfer_id
    return tx


def handle_transfer_webhook(reference: str, flw_status: str) -> None:
    tx = Transaction.objects.filter(
        reference=reference, tx_type=TransactionType.WITHDRAWAL
    ).first()
    if tx is None or tx.status != TransactionStatus.PENDING:
        return

    normalized = (flw_status or '').upper()
    if normalized == 'SUCCESSFUL':
        with transaction.atomic():
            locked = Transaction.objects.select_for_update().get(pk=tx.pk)
            if locked.status != TransactionStatus.PENDING:
                return
            locked.status = TransactionStatus.COMPLETED
            locked.save(update_fields=['status', 'updated_at'])
    elif normalized in ('FAILED', 'CANCELLED'):
        _refund_and_fail(tx.pk, f'Transfer {normalized.lower()} by Flutterwave.')


# ─── Reconciliation (missed transfer webhooks) ─────────────────────────────

RECONCILE_MIN_AGE = timedelta(minutes=2)  # give the webhook a chance first
_RECONCILE_MAX_PAGES = 10


def _find_transfers_by_reference(references: set[str]) -> dict[str, dict]:
    """Page through GET /transfers (newest first) until every reference is
    found or the page limit is hit — for withdrawals with no stored id."""
    found: dict[str, dict] = {}
    for page in range(1, _RECONCILE_MAX_PAGES + 1):
        payload = flutterwave.list_transfers(page)
        rows = payload.get('data') or []
        for row in rows:
            ref = str(row.get('reference') or '')
            if ref in references:
                found[ref] = row
        total_pages = ((payload.get('meta') or {}).get('page_info') or {}).get('total_pages') or page
        if len(found) == len(references) or not rows or page >= total_pages:
            break
    return found


def reconcile_pending_withdrawals(
    *, min_age: timedelta = RECONCILE_MIN_AGE, user=None
) -> list[tuple[Transaction, str]]:
    """Asks Flutterwave for the real status of every withdrawal still
    pending (optionally just one user's), and applies it exactly as the
    transfer.completed webhook would (SUCCESSFUL -> completed,
    FAILED/CANCELLED -> refunded + failed). A
    transfer Flutterwave has no record of is left pending and logged for an
    admin — never refunded automatically, since it may still have paid out.
    Returns (transaction, status before) for each one that changed."""
    cutoff = timezone.now() - min_age
    qs = Transaction.objects.filter(
        tx_type=TransactionType.WITHDRAWAL,
        status=TransactionStatus.PENDING,
        created_at__lte=cutoff,
    )
    if user is not None:
        qs = qs.filter(user=user)
    pending = list(qs)
    if not pending:
        return []

    by_reference: dict[str, dict] = {}
    missing_ids = {tx.reference for tx in pending if not tx.flw_transfer_id}
    if missing_ids:
        try:
            by_reference = _find_transfers_by_reference(missing_ids)
        except FlutterwaveError as exc:
            logger.warning('Withdrawal reconcile: could not list transfers: %s', exc.message)

    changed = []
    for tx in pending:
        data = by_reference.get(tx.reference)
        if tx.flw_transfer_id:
            try:
                data = (flutterwave.get_transfer(tx.flw_transfer_id).get('data')) or None
            except FlutterwaveError as exc:
                logger.warning('Withdrawal reconcile: could not fetch transfer %s: %s', tx.reference, exc.message)
                continue
        if not data:
            logger.warning('Withdrawal reconcile: Flutterwave has no transfer for %s yet.', tx.reference)
            continue

        transfer_id = str(data.get('id') or '')
        if transfer_id and not tx.flw_transfer_id:
            Transaction.objects.filter(pk=tx.pk).update(flw_transfer_id=transfer_id)

        flw_status = str(data.get('status') or '')
        handle_transfer_webhook(tx.reference, flw_status)
        tx.refresh_from_db()
        if tx.status != TransactionStatus.PENDING:
            logger.info('Withdrawal reconcile: %s -> %s (Flutterwave %s)', tx.reference, tx.status, flw_status)
            changed.append((tx, TransactionStatus.PENDING))
    return changed


# ─── Banks / account resolution ────────────────────────────────────────────


def list_banks() -> list[dict]:
    try:
        payload = flutterwave.get_banks()
    except FlutterwaveError as exc:
        raise WalletServiceError(
            f'Could not load banks: {exc.message}', code='flw_unreachable', status=502
        ) from exc

    data = payload.get('data') or []
    return [{'name': b.get('name'), 'code': b.get('code')} for b in data]


def resolve_account_name(account_number: str, bank_code: str) -> str:
    try:
        payload = flutterwave.resolve_account(account_number, bank_code)
    except FlutterwaveError as exc:
        raise WalletServiceError(
            f'Could not resolve account: {exc.message}', code='flw_unreachable', status=502
        ) from exc

    if payload.get('status') != 'success':
        raise WalletServiceError(
            payload.get('message', 'Could not resolve this account.'),
            code='resolve_failed',
        )

    data = payload.get('data') or {}
    account_name = data.get('account_name')
    if not account_name:
        raise WalletServiceError('Could not resolve this account.', code='resolve_failed')
    return account_name


_USER_RECONCILE_MIN_AGE = timedelta(seconds=30)
_USER_RECONCILE_THROTTLE = 30  # seconds between Flutterwave checks per user


def refresh_pending_withdrawals(user) -> None:
    """Checks this user's pending withdrawals against Flutterwave when they
    load their history, so a missed webhook (or a stopped worker) doesn't
    leave a paid-out withdrawal showing "pending". Throttled per user and
    never raises — a Flutterwave outage must not break the history list."""
    cache_key = f'wallet:withdrawal-refresh:{user.pk}'
    if not cache.add(cache_key, 1, _USER_RECONCILE_THROTTLE):
        return
    try:
        reconcile_pending_withdrawals(min_age=_USER_RECONCILE_MIN_AGE, user=user)
    except Exception:
        logger.exception('Withdrawal refresh failed for user %s', user.pk)


def list_transactions(user):
    refresh_pending_withdrawals(user)
    return Transaction.objects.filter(user=user).order_by('-created_at')[:50]
