"""Crypto withdrawals.

Unlike buy/sell/swap, withdrawals don't go through the quote engine —
architecture principle #4 scopes quotes to buy/sell/swap only, and the fee
is computed directly at request time instead of being locked in advance.
No admin approval step: the guards below (address format, min notional,
daily cap, balance reservation) are what make it safe to run unattended.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import quidax
from .models import (
    CryptoWithdrawal,
    CryptoWithdrawalLog,
    FeeType,
    WithdrawalStatus,
    generate_withdrawal_reference,
)
from .quidax import QuidaxError
from .services import (
    MIN_ORDER_NOTIONAL_NGN,
    CryptoServiceError,
    _validate_coin,
    check_transaction_pin,
    compute_fee_ngn,
    credit_crypto_available,
    debit_reserved_crypto,
    get_coin_rate_ngn,
    release_reserved_crypto,
    reserve_crypto,
)

# Loose but real per-(coin, network) address shape checks — enough to catch
# obvious typos before ever calling Quidax; Quidax remains the final
# authority on whether an address is actually valid. Coins not listed here
# (e.g. ton, ada's older Byron format) skip client-side validation and rely
# on that backstop instead of risking a wrong regex blocking a real address.
_EVM_ADDRESS = re.compile(r'^0x[a-fA-F0-9]{40}$')
_TRON_ADDRESS = re.compile(r'^T[a-zA-Z0-9]{33}$')

_ADDRESS_PATTERNS: dict[tuple[str, str], re.Pattern] = {
    ('btc', ''): re.compile(r'^(bc1[a-z0-9]{25,59}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$'),
    ('eth', ''): _EVM_ADDRESS,
    ('usdt', 'ERC20'): _EVM_ADDRESS,
    ('usdt', 'TRC20'): _TRON_ADDRESS,
    ('usdc', 'ERC20'): _EVM_ADDRESS,
    ('usdc', 'TRC20'): _TRON_ADDRESS,
    ('sol', ''): re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$'),
    ('trx', ''): _TRON_ADDRESS,
    ('ltc', ''): re.compile(r'^(ltc1[a-z0-9]{25,59}|[LM3][a-km-zA-HJ-NP-Z1-9]{25,34})$'),
    ('dash', ''): re.compile(r'^X[a-km-zA-HJ-NP-Z1-9]{33}$'),
    ('doge', ''): re.compile(r'^D[a-km-zA-HJ-NP-Z1-9]{33}$'),
    ('bch', ''): re.compile(r'^((bitcoincash:)?[qp][a-z0-9]{41}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$'),
    ('bnb', ''): _EVM_ADDRESS,  # BNB Smart Chain (BEP20) — same shape as ETH
    ('link', ''): _EVM_ADDRESS,  # ERC20 token
    ('etc', ''): _EVM_ADDRESS,
    ('sand', ''): _EVM_ADDRESS,  # ERC20 token
    ('shib', ''): _EVM_ADDRESS,  # ERC20 token
    ('aave', ''): _EVM_ADDRESS,  # ERC20 token
    ('pol', ''): _EVM_ADDRESS,  # Polygon PoS — EVM-compatible
    ('xrp', ''): re.compile(r'^r[a-km-zA-HJ-NP-Z1-9]{24,34}$'),
    ('ada', ''): re.compile(r'^addr1[a-z0-9]{20,103}$'),
    ('dot', ''): re.compile(r'^1[a-km-zA-HJ-NP-Z1-9]{46,47}$'),
    ('xlm', ''): re.compile(r'^G[A-Z2-7]{55}$'),
    ('near', ''): re.compile(r'^([a-f0-9]{64}|[a-z0-9_\-.]{2,64}\.near)$'),
    ('sui', ''): re.compile(r'^0x[a-fA-F0-9]{64}$'),
    ('fil', ''): re.compile(r'^f[1234][a-zA-Z0-9]{20,86}$'),
    ('algo', ''): re.compile(r'^[A-Z2-7]{58}$'),
}


def _validate_address(coin: str, network: str, address: str) -> None:
    pattern = _ADDRESS_PATTERNS.get((coin, network.upper())) or _ADDRESS_PATTERNS.get((coin, ''))
    if pattern and not pattern.match(address.strip()):
        raise CryptoServiceError(
            'This does not look like a valid address for this network.', code='invalid_address'
        )


def _log(withdrawal: CryptoWithdrawal, event: str, detail: str = '') -> None:
    CryptoWithdrawalLog.objects.create(withdrawal=withdrawal, event=event, detail=detail)


def _existing_withdrawal_for_idempotency_key(idempotency_key: str | None) -> CryptoWithdrawal | None:
    if not idempotency_key:
        return None
    return CryptoWithdrawal.objects.filter(idempotency_key=idempotency_key).first()


def _daily_withdrawn_ngn(user) -> Decimal:
    """Sum of today's processing+completed withdrawals, valued at each
    withdrawal's own rate snapshot (not today's live rate) — consistent
    with the "never recompute after the fact" principle used elsewhere."""
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = CryptoWithdrawal.objects.filter(
        user=user,
        status__in=[WithdrawalStatus.PROCESSING, WithdrawalStatus.COMPLETED],
        created_at__gte=today_start,
    ).values_list('rate_ngn', 'amount')
    return sum((rate * amount for rate, amount in rows), Decimal('0'))


def _withdrawal_volume(withdrawal: CryptoWithdrawal) -> str:
    """Plain decimal string, no scientific notation — same formatting rule as sell."""
    normalized = withdrawal.net_amount.normalize()
    _sign, _digits, exponent = normalized.as_tuple()
    if exponent >= 0:
        return str(int(normalized))
    return format(normalized, 'f')


def _create_withdrawal_row(
    user,
    *,
    coin: str,
    network: str,
    address: str,
    amount: Decimal,
    fee_coin: Decimal,
    net_amount: Decimal,
    rate_ngn: Decimal,
    idempotency_key: str | None,
) -> CryptoWithdrawal:
    for _ in range(5):
        reference = generate_withdrawal_reference()
        try:
            return CryptoWithdrawal.objects.create(
                reference=reference,
                idempotency_key=idempotency_key or None,
                user=user,
                coin=coin,
                network=network,
                address=address,
                amount=amount,
                fee_coin=fee_coin,
                net_amount=net_amount,
                rate_ngn=rate_ngn,
                status=WithdrawalStatus.PROCESSING,
            )
        except IntegrityError:
            continue
    raise CryptoServiceError('Could not generate a withdrawal reference.', status=500)


def _execute_quidax_withdrawal(withdrawal: CryptoWithdrawal) -> CryptoWithdrawal:
    volume = _withdrawal_volume(withdrawal)

    try:
        payload = quidax.create_withdrawal(
            currency=withdrawal.coin,
            amount=volume,
            address=withdrawal.address,
            network=withdrawal.network or None,
            reference=withdrawal.reference,
            user_id=settings.QUIDAX_USER_ID,
        )
    except QuidaxError as exc:
        _log(withdrawal, 'quidax_error', f'Withdrawal request failed: {exc.message}')
        release_reserved_crypto(withdrawal.user, withdrawal.coin, withdrawal.amount)
        _log(
            withdrawal,
            'reservation_released',
            f'Released {withdrawal.amount} {withdrawal.coin.upper()} back to available.',
        )
        with transaction.atomic():
            locked = CryptoWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
            if locked.status != WithdrawalStatus.PROCESSING:
                return locked
            locked.status = WithdrawalStatus.FAILED
            locked.note = f'Quidax withdrawal failed: {exc.message}'
            locked.save(update_fields=['status', 'note', 'updated_at'])
        withdrawal.refresh_from_db()
        return withdrawal

    data = payload.get('data') or {}
    quidax_withdrawal_id = str(data.get('id') or '')

    # Quidax accepted the request — commit the reservation. Final
    # confirmation (tx_id, actually completed on-chain) comes via webhook;
    # status stays processing until then.
    with transaction.atomic():
        locked = CryptoWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
        locked.quidax_withdrawal_id = quidax_withdrawal_id or locked.quidax_withdrawal_id
        locked.save(update_fields=['quidax_withdrawal_id', 'updated_at'])
        debit_reserved_crypto(locked.user, locked.coin, locked.amount)

    _log(
        withdrawal,
        'quidax_withdrawal_sent',
        f'volume={volume} quidax_withdrawal_id={quidax_withdrawal_id}',
    )
    withdrawal.refresh_from_db()
    return withdrawal


def estimate_withdrawal(*, coin: str, amount: Decimal) -> dict:
    """Read-only fee/net-amount preview — no reservation, no PIN, no side
    effects. Lets a client show an accurate summary before the user
    confirms, since (unlike buy/sell/swap) withdrawals have no quote to
    lock the numbers in advance."""
    coin = _validate_coin(coin)
    if amount is None or amount <= 0:
        raise CryptoServiceError('Amount must be greater than zero.', code='invalid_amount')

    rate = get_coin_rate_ngn(coin)
    notional_ngn = (rate * amount).quantize(Decimal('0.01'))
    if notional_ngn < MIN_ORDER_NOTIONAL_NGN:
        raise CryptoServiceError(
            f'Minimum withdrawal amount is ₦{MIN_ORDER_NOTIONAL_NGN}.', code='amount_too_low'
        )

    fee_ngn = compute_fee_ngn(FeeType.WITHDRAW, notional_ngn)
    fee_coin = (
        (fee_ngn / rate).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        if fee_ngn > 0
        else Decimal('0')
    )
    net_amount = amount - fee_coin
    if net_amount <= 0:
        raise CryptoServiceError('Amount too small after fees.', code='amount_too_low')

    return {
        'coin': coin,
        'amount': str(amount),
        'rate_ngn': str(rate),
        'notional_ngn': str(notional_ngn),
        'fee_coin': str(fee_coin),
        'fee_ngn': str(fee_ngn),
        'net_amount': str(net_amount),
    }


def request_withdrawal(
    user,
    *,
    coin: str,
    amount: Decimal,
    address: str,
    pin: str,
    network: str | None = None,
    idempotency_key: str | None = None,
) -> CryptoWithdrawal:
    existing = _existing_withdrawal_for_idempotency_key(idempotency_key)
    if existing:
        return existing

    check_transaction_pin(user, pin)

    address = address.strip()
    network = (network or '').upper()

    estimate = estimate_withdrawal(coin=coin, amount=amount)
    coin = estimate['coin']
    _validate_address(coin, network, address)

    rate = Decimal(estimate['rate_ngn'])
    notional_ngn = Decimal(estimate['notional_ngn'])
    fee_coin = Decimal(estimate['fee_coin'])
    net_amount = Decimal(estimate['net_amount'])

    daily_limit = Decimal(str(settings.CRYPTO_WITHDRAW_DAILY_LIMIT_NGN))
    if _daily_withdrawn_ngn(user) + notional_ngn > daily_limit:
        raise CryptoServiceError(
            f'This would exceed your daily withdrawal limit of ₦{daily_limit}.',
            code='daily_limit_exceeded',
        )

    reserve_crypto(user, coin, amount)  # raises insufficient_balance

    withdrawal = _create_withdrawal_row(
        user,
        coin=coin,
        network=network,
        address=address,
        amount=amount,
        fee_coin=fee_coin,
        net_amount=net_amount,
        rate_ngn=rate,
        idempotency_key=idempotency_key,
    )
    _log(
        withdrawal,
        'withdrawal_created',
        f'{amount} {coin.upper()} to {address} (fee {fee_coin} {coin.upper()})',
    )
    _log(withdrawal, 'reserved_balance', f'Reserved {amount} {coin.upper()}.')

    return _execute_quidax_withdrawal(withdrawal)


def list_withdrawals(user):
    return CryptoWithdrawal.objects.filter(user=user).order_by('-created_at')[:50]


# ─── Webhooks ───────────────────────────────────────────────────────────────


def handle_withdraw_webhook(payload: dict, *, rejected: bool) -> None:
    """withdraw.successful -> completed + tx_id. withdraw.rejected ->
    rejected + credit the full gross amount back to available."""
    data = payload.get('data') or payload
    quidax_withdrawal_id = str(data.get('id') or '')
    reference = data.get('reference')
    tx_id = str(data.get('txid') or data.get('tx_id') or '')

    withdrawal = None
    if quidax_withdrawal_id:
        withdrawal = CryptoWithdrawal.objects.filter(
            quidax_withdrawal_id=quidax_withdrawal_id
        ).first()
    if withdrawal is None and reference:
        withdrawal = CryptoWithdrawal.objects.filter(reference=reference).first()
    if withdrawal is None or withdrawal.status != WithdrawalStatus.PROCESSING:
        return

    with transaction.atomic():
        locked = CryptoWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
        if locked.status != WithdrawalStatus.PROCESSING:
            return

        if rejected:
            locked.status = WithdrawalStatus.REJECTED
            locked.save(update_fields=['status', 'updated_at'])
            credit_crypto_available(locked.user, locked.coin, locked.amount)
        else:
            locked.status = WithdrawalStatus.COMPLETED
            locked.tx_id = tx_id
            locked.save(update_fields=['status', 'tx_id', 'updated_at'])

    if rejected:
        _log(withdrawal, 'withdraw_rejected', f'Credited {withdrawal.amount} {withdrawal.coin.upper()} back to available.')
    else:
        _log(withdrawal, 'withdraw_completed', f'tx_id={tx_id}')


# ─── Admin operations (ops safety net) ─────────────────────────────────────


def admin_complete_withdrawal(
    withdrawal: CryptoWithdrawal, tx_id: str = '', note: str = ''
) -> CryptoWithdrawal:
    """Manually completes a processing withdrawal — for when Quidax's
    webhook never arrived but the on-chain transfer is confirmed some
    other way (block explorer, Quidax dashboard)."""
    if withdrawal.status != WithdrawalStatus.PROCESSING:
        raise CryptoServiceError(
            'Only processing withdrawals can be manually completed.', code='invalid_state'
        )

    with transaction.atomic():
        locked = CryptoWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
        if locked.status != WithdrawalStatus.PROCESSING:
            return locked
        locked.status = WithdrawalStatus.COMPLETED
        if tx_id:
            locked.tx_id = tx_id
        locked.save(update_fields=['status', 'tx_id', 'updated_at'])

    _log(withdrawal, 'admin_manual_complete', f'tx_id={tx_id}. {note}'.strip())
    withdrawal.refresh_from_db()
    return withdrawal


def admin_reject_withdrawal(withdrawal: CryptoWithdrawal, note: str = '') -> CryptoWithdrawal:
    """Manually rejects a processing withdrawal and credits the gross
    amount back — for when the webhook never arrived but Quidax/on-chain
    data shows it actually failed."""
    if withdrawal.status != WithdrawalStatus.PROCESSING:
        raise CryptoServiceError(
            'Only processing withdrawals can be manually rejected.', code='invalid_state'
        )

    with transaction.atomic():
        locked = CryptoWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)
        if locked.status != WithdrawalStatus.PROCESSING:
            return locked
        locked.status = WithdrawalStatus.REJECTED
        locked.save(update_fields=['status', 'updated_at'])
        credit_crypto_available(locked.user, locked.coin, locked.amount)

    _log(withdrawal, 'admin_manual_reject', note or 'Manually rejected by admin.')
    withdrawal.refresh_from_db()
    return withdrawal
