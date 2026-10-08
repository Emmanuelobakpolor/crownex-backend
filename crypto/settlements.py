"""Deposit settlement: sweep every credited deposit from the user's Quidax
sub-account to the master account, then — if the user had auto-convert on
when it arrived — sell it to NGN at the live rate.

Why the sweep exists at all: deposits land in per-user sub-accounts, but
every sell, swap and withdrawal executes on the master account
(settings.QUIDAX_USER_ID). Without it the master pays out coins it never
received while the deposits pile up in sub-accounts.

Nothing here runs inside the webhook request. deposits._credit_deposit
creates the CryptoDepositSettlement row (in the same transaction as the
credit), and the process_deposit_settlements worker drives it:

    PENDING  --sweep sent-->  SWEEPING  --Quidax done-->  SWEPT
    SWEPT    --hold------------------------------------>  COMPLETED
    SWEPT    --sell sent-->   SELLING   --accepted----->  COMPLETED
    SWEPT    --below min / no NGN market--------------->  SKIPPED

Rules that make retries safe:
  * Every state change is a compare-and-set on the current status, so two
    workers (or a worker and a webhook) can never both act on one step.
  * A sweep is only ever re-sent with a NEW reference after Quidax has
    definitely rejected or never received the previous one. Any ambiguous
    outcome is resolved by looking the reference up, never by re-sending.
  * Nothing sells until Quidax reports the sweep done (swept_at is set).
  * A sell whose outcome is unknown (timeout / 5xx) is never retried
    automatically — it goes to FAILED for an admin to check on Quidax.
  * The NGN credit, the reserved-crypto debit and COMPLETED are one DB
    transaction (orders.complete_sell + the status change).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import quidax
from .models import (
    CryptoDepositEvent,
    CryptoDepositSettlement,
    CryptoDepositSettlementLog,
    OrderStatus,
    QuidaxSubAccount,
    QuoteType,
    SettlementStatus,
)
from .orders import _create_order_from_quote, _fail_order, complete_sell, send_master_sell
from .orders import _log as _log_order
from .quidax import QuidaxError
from .services import (
    SUPPORTED_COINS,
    CryptoServiceError,
    create_quote,
    mark_quote_used,
    release_reserved_crypto,
    reserve_crypto,
)

logger = logging.getLogger(__name__)

SWEEP_REFERENCE_PREFIX = 'CRYSWP-'
MAX_ATTEMPTS = 8
SWEEP_POLL = timedelta(seconds=30)
# A sweep Quidax has no record of this long after we sent it never landed.
SWEEP_LOOKUP_GRACE = timedelta(minutes=10)
# Quidax still says processing after this long: hand it to an admin.
SWEEP_TIMEOUT = timedelta(hours=24)
# A SELLING row nobody finished (worker died mid-call) is past this.
SELL_STALE_AFTER = timedelta(minutes=10)

ACTIVE_STATUSES = (
    SettlementStatus.PENDING,
    SettlementStatus.SWEEPING,
    SettlementStatus.SWEPT,
    SettlementStatus.SELLING,
)

_master_account_id_cache: str | None = None


class _LostRace(Exception):
    """Another worker moved the row first — roll back and leave it."""


# ─── Helpers ────────────────────────────────────────────────────────────────


def _event(s: CryptoDepositSettlement, event: str, detail: str = '', *, level=logging.INFO, **ids) -> None:
    """Audit row + one structured log line with only safe identifiers."""
    CryptoDepositSettlementLog.objects.create(settlement=s, event=event, detail=detail)
    fields = {
        'settlement': s.pk,
        'user': s.user_id,
        'deposit': s.deposit.quidax_deposit_id,
        'coin': s.coin,
        'amount': s.amount,
        **ids,
    }
    logger.log(level, '%s %s %s', event, ' '.join(f'{k}={v}' for k, v in fields.items()), detail)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(60 * 2 ** max(attempts - 1, 0), 3600))


def _transition(s: CryptoDepositSettlement, from_status: str, **fields) -> bool:
    """Compare-and-set: applies `fields` only if the row is still in
    from_status. Works the same on Postgres and SQLite."""
    fields['updated_at'] = timezone.now()
    updated = CryptoDepositSettlement.objects.filter(pk=s.pk, status=from_status).update(**fields)
    s.refresh_from_db()
    return bool(updated)


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def master_account_id() -> str:
    global _master_account_id_cache
    configured = getattr(settings, 'QUIDAX_MASTER_ACCOUNT_ID', '')
    if configured:
        return configured
    if _master_account_id_cache is None:
        data = quidax.get_master_account().get('data') or {}
        master_id = str(data.get('id') or '')
        if not master_id:
            raise QuidaxError('Quidax GET /users/me returned no account id.')
        _master_account_id_cache = master_id
    return _master_account_id_cache


def _has_ngn_market(coin: str) -> bool:
    """Sells go through the {coin}ngn market (orders.send_master_sell); coins
    Quidax only lists against USDT have none."""
    return not SUPPORTED_COINS.get(coin, {}).get('via_usdt')


# ─── Creation (called from the deposit webhook's transaction) ───────────────


def create_for_deposit(event) -> CryptoDepositSettlement:
    """Must run inside the same transaction that credits the deposit, so the
    credit and its settlement commit together or not at all. Pure DB work."""
    user = event.user
    s = CryptoDepositSettlement.objects.create(
        deposit=event,
        user=user,
        coin=event.coin,
        amount=event.amount,
        auto_convert=bool(user.auto_convert_crypto_deposits),
    )
    if s.auto_convert:
        try:
            reserve_crypto(user, s.coin, s.amount)
        except CryptoServiceError:
            s.auto_convert = False
        else:
            s.reserved = True
        s.save(update_fields=['auto_convert', 'reserved', 'updated_at'])

    _event(s, 'settlement_created', 'auto-convert on' if s.auto_convert else 'hold')
    if s.auto_convert:
        _event(s, 'auto_convert_started', f'Reserved {s.amount} {s.coin.upper()} for conversion.')
    return s


def deposits_without_settlement():
    """Deposits credited before settlements existed — their coins are still
    in the users' sub-accounts."""
    return (
        CryptoDepositEvent.objects.filter(settlement__isnull=True)
        .select_related('user')
        .order_by('created_at')
    )


def create_backfill_sweep(event) -> CryptoDepositSettlement | None:
    """Sweep-only settlement for an old deposit. Never converts: the user
    may long since have spent, sold or withdrawn that balance internally
    (withdrawals and sells already came out of the master account), so the
    only thing left to do is move the coins where they're owed. Returns
    None if one already exists (safe to re-run)."""
    s, created = CryptoDepositSettlement.objects.get_or_create(
        deposit=event,
        defaults={'user': event.user, 'coin': event.coin, 'amount': event.amount, 'auto_convert': False},
    )
    if not created:
        return None
    _event(s, 'settlement_created', 'backfill: sweep only')
    return s


def _release_reservation(s: CryptoDepositSettlement, why: str) -> None:
    """Gives the deposit back to the user's available crypto, at most once."""
    with transaction.atomic():
        locked = CryptoDepositSettlement.objects.select_for_update().get(pk=s.pk)
        if not locked.reserved:
            return
        release_reserved_crypto(locked.user, locked.coin, locked.amount)
        locked.reserved = False
        locked.save(update_fields=['reserved', 'updated_at'])
    s.refresh_from_db()
    _event(s, 'reservation_released', why)


def _fail(s: CryptoDepositSettlement, from_status: str, reason: str, *, release: bool) -> None:
    if not _transition(s, from_status, status=SettlementStatus.FAILED, last_error=reason[:2000]):
        return
    if release:
        _release_reservation(s, 'Conversion abandoned — the user keeps the crypto.')
    _event(s, 'settlement_failed', reason, level=logging.ERROR)


# ─── Sweep ──────────────────────────────────────────────────────────────────


def _retry_sweep_later(s: CryptoDepositSettlement, from_status: str, reason: str) -> None:
    attempts = s.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        _fail(s, from_status, f'Sweep gave up after {attempts} attempts: {reason}', release=True)
        return
    if _transition(
        s,
        from_status,
        status=SettlementStatus.PENDING,
        attempts=attempts,
        next_attempt_at=timezone.now() + _backoff(attempts),
        last_error=reason[:2000],
    ):
        _event(s, 'sweep_failed', reason, level=logging.WARNING, attempt=attempts)
        _event(s, 'auto_convert_retry' if s.auto_convert else 'sweep_retry', f'next at {s.next_attempt_at.isoformat()}')


def _start_sweep(s: CryptoDepositSettlement) -> None:
    sub = QuidaxSubAccount.objects.filter(user=s.user).first()
    if sub is None:
        _fail(s, SettlementStatus.PENDING, 'User has no Quidax sub-account.', release=True)
        return

    try:
        master_id = master_account_id()
    except QuidaxError as exc:
        _retry_sweep_later(s, SettlementStatus.PENDING, f'Could not resolve master account: {exc.message}')
        return
    if master_id in (sub.quidax_user_id, 'me'):
        _fail(s, SettlementStatus.PENDING, 'Master account id is misconfigured.', release=True)
        return

    # The deposit must actually be spendable in the sub-account before we
    # try to move it (covers "credited by webhook but not yet settled").
    try:
        wallet = quidax.get_wallet(sub.quidax_user_id, s.coin).get('data') or {}
    except QuidaxError as exc:
        _retry_sweep_later(s, SettlementStatus.PENDING, f'Could not read sub-account balance: {exc.message}')
        return
    balance = _decimal(wallet.get('balance'))
    if balance is None or balance < s.amount:
        _retry_sweep_later(
            s, SettlementStatus.PENDING, f'Sub-account {s.coin.upper()} balance {balance} < {s.amount}.'
        )
        return

    reference = f'{SWEEP_REFERENCE_PREFIX}{s.pk}-{s.sweep_attempt + 1}'
    now = timezone.now()
    if not _transition(
        s,
        SettlementStatus.PENDING,
        status=SettlementStatus.SWEEPING,
        sweep_attempt=s.sweep_attempt + 1,
        sweep_reference=reference,
        quidax_sweep_id='',
        step_started_at=now,
        next_attempt_at=now + SWEEP_POLL,
    ):
        return
    _event(s, 'sweep_started', f'{s.amount} {s.coin.upper()} -> master', sub_account=sub.quidax_user_id, reference=reference)

    try:
        payload = quidax.create_internal_transfer(
            from_user_id=sub.quidax_user_id,
            to_user_id=master_id,
            currency=s.coin,
            amount=format(s.amount.normalize(), 'f'),
            reference=reference,
        )
    except QuidaxError as exc:
        _resolve_failed_sweep_request(s, sub, exc)
        return

    _apply_sweep_result(s, payload.get('data') or {})


def _resolve_failed_sweep_request(s: CryptoDepositSettlement, sub: QuidaxSubAccount, exc: QuidaxError) -> None:
    """The create call errored — but with _request's own retries, Quidax may
    still have accepted it. Only Quidax's record of the reference decides."""
    _event(s, 'sweep_request_error', exc.message, level=logging.WARNING, status_code=exc.status_code)
    found = _lookup_sweep(s, sub)
    if found:
        _apply_sweep_result(s, found)
        return
    definite = exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 429
    if definite and found is not None:
        # Rejected outright and Quidax confirms no withdrawal exists for it.
        _retry_sweep_later(s, SettlementStatus.SWEEPING, f'Quidax rejected sweep: {exc.message}')
        return
    s.last_error = f'Sweep request outcome unknown: {exc.message}'[:2000]
    s.save(update_fields=['last_error', 'updated_at'])  # stays SWEEPING; the poll resolves it


def _lookup_sweep(s: CryptoDepositSettlement, sub: QuidaxSubAccount) -> dict | None:
    """{}: Quidax answered and has no such reference. None: couldn't ask."""
    try:
        payload = quidax.get_withdrawal_by_reference(sub.quidax_user_id, s.sweep_reference)
    except QuidaxError as exc:
        if exc.status_code == 404:
            return {}
        return None
    data = payload.get('data')
    return data if isinstance(data, dict) and data.get('reference') == s.sweep_reference else {}


def _apply_sweep_result(s: CryptoDepositSettlement, data: dict) -> None:
    status = str(data.get('status') or '').lower()
    quidax_id = str(data.get('id') or '')

    if status == 'done':
        currency = str(data.get('currency') or s.coin).lower()
        amount = _decimal(data.get('amount'))
        if currency != s.coin or amount != s.amount:
            _fail(
                s,
                SettlementStatus.SWEEPING,
                f'Quidax sweep {quidax_id} reports {amount} {currency}, expected {s.amount} {s.coin}.',
                release=False,
            )
            return
        if _transition(
            s,
            SettlementStatus.SWEEPING,
            status=SettlementStatus.SWEPT,
            quidax_sweep_id=quidax_id,
            swept_at=timezone.now(),
            attempts=0,
            last_error='',
            next_attempt_at=timezone.now(),
        ):
            fee = _decimal(data.get('fee')) or Decimal('0')
            _event(s, 'sweep_completed', f'fee={fee}', level=logging.WARNING if fee else logging.INFO, quidax_sweep=quidax_id)
        return

    if status == 'rejected':
        reason = str(data.get('reason') or 'no reason given')
        _retry_sweep_later(s, SettlementStatus.SWEEPING, f'Quidax rejected sweep {quidax_id}: {reason}')
        return

    # processing / submitted / anything not final: keep polling.
    if quidax_id and not s.quidax_sweep_id:
        s.quidax_sweep_id = quidax_id
        s.save(update_fields=['quidax_sweep_id', 'updated_at'])


def _poll_sweep(s: CryptoDepositSettlement) -> None:
    sub = QuidaxSubAccount.objects.filter(user=s.user).first()
    age = timezone.now() - (s.step_started_at or s.updated_at)
    found = _lookup_sweep(s, sub) if sub else None

    if found:
        _apply_sweep_result(s, found)
        s.refresh_from_db()
        if s.status == SettlementStatus.SWEEPING and age > SWEEP_TIMEOUT:
            _fail(s, SettlementStatus.SWEEPING, 'Sweep still processing on Quidax after 24h.', release=True)
    elif found is not None and age > SWEEP_LOOKUP_GRACE:
        # Quidax has no withdrawal with this reference well after we sent it.
        # Deliberately NOT re-sent automatically: an admin confirms on the
        # Quidax dashboard, then `--retry` resends with a fresh reference.
        _fail(s, SettlementStatus.SWEEPING, f'Quidax has no record of sweep {s.sweep_reference}.', release=True)
        return
    elif found is None and age > SWEEP_TIMEOUT:
        _fail(s, SettlementStatus.SWEEPING, f'Could not confirm sweep {s.sweep_reference} for 24h.', release=True)
        return

    s.refresh_from_db()
    if s.status == SettlementStatus.SWEEPING:
        _transition(s, SettlementStatus.SWEEPING, next_attempt_at=timezone.now() + SWEEP_POLL)


def handle_sweep_webhook(payload: dict, *, rejected: bool) -> bool:
    """withdraw.successful / withdraw.rejected for one of our sweeps. Returns
    False if the event isn't a sweep, so the caller can route it to the
    regular withdrawal handler. Only records the result — any sell happens
    in the worker, not in the webhook request."""
    data = payload.get('data') or payload
    reference = str(data.get('reference') or '')
    if not reference.startswith(SWEEP_REFERENCE_PREFIX):
        return False

    s = CryptoDepositSettlement.objects.filter(sweep_reference=reference).first()
    if s is None or s.status != SettlementStatus.SWEEPING:
        return True
    _apply_sweep_result(s, {**data, 'status': 'rejected' if rejected else 'done'})
    return True


# ─── Sell ───────────────────────────────────────────────────────────────────


def _skip_conversion(s: CryptoDepositSettlement, reason: str) -> None:
    if _transition(s, SettlementStatus.SWEPT, status=SettlementStatus.SKIPPED, last_error=reason):
        _release_reservation(s, 'Not converted — the user keeps the crypto.')
        _event(s, 'auto_convert_skipped', reason)


def _retry_sell_later(s: CryptoDepositSettlement, reason: str) -> None:
    """Back to SWEPT (coins confirmed in master, still reserved) for another
    sell attempt — the sweep is never repeated."""
    attempts = s.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        _fail(s, SettlementStatus.SELLING, f'Sell gave up after {attempts} attempts: {reason}', release=True)
        return
    if _transition(
        s,
        SettlementStatus.SELLING,
        status=SettlementStatus.SWEPT,
        attempts=attempts,
        next_attempt_at=timezone.now() + _backoff(attempts),
        last_error=reason[:2000],
    ):
        _event(s, 'auto_convert_retry', f'{reason}; next at {s.next_attempt_at.isoformat()}', attempt=attempts)


def _after_sweep(s: CryptoDepositSettlement) -> None:
    if not s.auto_convert:
        if _transition(s, SettlementStatus.SWEPT, status=SettlementStatus.COMPLETED):
            _event(s, 'settlement_completed', 'Swept; user holds the crypto.')
        return

    if not _has_ngn_market(s.coin):
        _skip_conversion(s, f'No {s.coin.upper()}/NGN market to sell into.')
        return

    # Fresh quote at execution time = live rate + the normal sell fee.
    try:
        quote = create_quote(s.user, quote_type=QuoteType.SELL, coin=s.coin, amount=s.amount)
    except CryptoServiceError as exc:
        if exc.code == 'amount_too_low':
            _skip_conversion(s, f'Below minimum sell amount: {exc.message}')
            return
        attempts = s.attempts + 1
        _transition(
            s,
            SettlementStatus.SWEPT,
            attempts=attempts,
            next_attempt_at=timezone.now() + _backoff(attempts),
            last_error=exc.message,
        )
        _event(s, 'auto_convert_retry', f'Quote failed: {exc.message}', level=logging.WARNING)
        return
    if quote.total_ngn <= 0:
        _skip_conversion(s, f'Sell fee ₦{quote.fee_ngn} leaves no payout.')
        return

    now = timezone.now()
    try:
        with transaction.atomic():
            mark_quote_used(quote)
            order = _create_order_from_quote(
                s.user, quote, idempotency_key=f'AUTOCNV-{s.pk}-{int(time.time() * 1000)}'
            )
            if not _transition(
                s,
                SettlementStatus.SWEPT,
                status=SettlementStatus.SELLING,
                order=order,
                step_started_at=now,
                next_attempt_at=now + SELL_STALE_AFTER,
            ):
                raise _LostRace
    except _LostRace:
        return

    _log_order(order, 'order_created', f'Auto-convert deposit {s.deposit.quidax_deposit_id}: sell {order.coin_amount} {order.coin.upper()} for ~₦{order.total_ngn}')
    _log_order(order, 'reserved_balance', f'Reserved at deposit time (settlement {s.pk}).')
    _event(s, 'sell_started', f'~₦{order.total_ngn} at ₦{order.rate_ngn}', order=order.reference)

    try:
        quidax_order_id = send_master_sell(order, retry=False)
    except QuidaxError as exc:
        _log_order(order, 'quidax_error', f'Sell order request failed: {exc.message}')
        _fail_order(order, f'Quidax sell failed: {exc.message}', refund_ngn=False)
        _event(s, 'sell_failed', exc.message, level=logging.WARNING, order=order.reference, status_code=exc.status_code)
        definite = exc.status_code is not None and 400 <= exc.status_code < 500
        if definite:
            _retry_sell_later(s, f'Quidax rejected sell: {exc.message}')
        else:
            # Quidax may have executed it — never auto-resell; keep the
            # crypto reserved until an admin checks the master account.
            _fail(s, SettlementStatus.SELLING, f'Sell outcome unknown ({order.reference}): {exc.message}', release=False)
        return

    with transaction.atomic():
        order = complete_sell(order, quidax_order_id)
        if order.status != OrderStatus.COMPLETED or not _transition(
            s, SettlementStatus.SELLING, status=SettlementStatus.COMPLETED, reserved=False, last_error=''
        ):
            raise RuntimeError(f'Settlement {s.pk} left SELLING while its sell was in flight.')

    _event(s, 'sell_completed', f'₦{order.total_ngn}', order=order.reference, quidax_order=quidax_order_id)
    _event(s, 'auto_convert_completed', f'Credited ₦{order.total_ngn} to NGN wallet.', order=order.reference)


def _check_stale_sell(s: CryptoDepositSettlement) -> None:
    age = timezone.now() - (s.step_started_at or s.updated_at)
    if age > SELL_STALE_AFTER:
        _fail(s, SettlementStatus.SELLING, 'Worker stopped mid-sell; check the Quidax master account.', release=False)


# ─── Worker entry points ────────────────────────────────────────────────────

_STEPS = {
    SettlementStatus.PENDING: _start_sweep,
    SettlementStatus.SWEEPING: _poll_sweep,
    SettlementStatus.SWEPT: _after_sweep,
    SettlementStatus.SELLING: _check_stale_sell,
}


def process_settlement(pk: int) -> CryptoDepositSettlement:
    """Advances one settlement as far as it can go right now (e.g. a sweep
    that comes back done immediately goes straight on to the sell)."""
    s = CryptoDepositSettlement.objects.get(pk=pk)
    for _ in range(len(_STEPS)):
        if s.status not in ACTIVE_STATUSES or s.next_attempt_at > timezone.now():
            break
        before = s.status
        _STEPS[before](s)
        s.refresh_from_db()
        if s.status == before:
            break
    return s


def process_due(limit: int = 50) -> int:
    ids = list(
        CryptoDepositSettlement.objects.filter(
            status__in=ACTIVE_STATUSES, next_attempt_at__lte=timezone.now()
        )
        .order_by('next_attempt_at')
        .values_list('pk', flat=True)[:limit]
    )
    for pk in ids:
        try:
            process_settlement(pk)
        except Exception:
            # One bad row must not stall the queue; it stays in its last
            # committed state and is retried on a later pass.
            logger.exception('settlement_processing_error settlement=%s', pk)
    return len(ids)


def admin_retry(s: CryptoDepositSettlement) -> CryptoDepositSettlement:
    """Resumes a FAILED settlement after an admin has checked Quidax:
    from SWEPT if the sweep was confirmed done, else from PENDING with a
    fresh sweep reference. Re-reserves for auto-convert if still possible;
    otherwise it completes as a plain sweep."""
    if s.status != SettlementStatus.FAILED:
        raise CryptoServiceError('Only failed settlements can be retried.', code='invalid_state')

    resume = SettlementStatus.SWEPT if s.swept_at else SettlementStatus.PENDING
    with transaction.atomic():
        locked = CryptoDepositSettlement.objects.select_for_update().get(pk=s.pk)
        if locked.status != SettlementStatus.FAILED:
            return locked
        if locked.auto_convert and not locked.reserved:
            try:
                reserve_crypto(locked.user, locked.coin, locked.amount)
                locked.reserved = True
            except CryptoServiceError:
                locked.auto_convert = False
        locked.status = resume
        locked.attempts = 0
        locked.next_attempt_at = timezone.now()
        locked.save()
    s.refresh_from_db()
    _event(s, 'admin_retry', f'Resuming from {resume}; auto_convert={s.auto_convert}.')
    return s
