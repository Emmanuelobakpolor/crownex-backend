"""Order execution: buy (this phase), sell and swap (later phases).

Every path here starts from an already-locked CryptoQuote — rate, fee, and
amounts are always copied from the quote, never recomputed here. See
services.py for quote creation/locking and models.py for the CryptoOrder
status lifecycle.
"""

from __future__ import annotations

import logging
import time
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction

from wallet import flutterwave
from wallet.flutterwave import FlutterwaveError
from wallet.services import WalletServiceError, credit_wallet, debit_wallet

from . import quidax
from .models import (
    CryptoOrder,
    CryptoOrderLog,
    OrderStatus,
    OrderType,
    QuoteType,
    SwapMethod,
    generate_order_reference,
)
from .quidax import QuidaxError
from .services import (
    CryptoServiceError,
    check_transaction_pin,
    credit_crypto_available,
    debit_reserved_crypto,
    get_locked_quote,
    mark_quote_used,
    plain_decimal,
    quidax_decimal,
    release_reserved_crypto,
    reserve_crypto,
)

logger = logging.getLogger(__name__)


def _log(order: CryptoOrder, event: str, detail: str = '') -> None:
    CryptoOrderLog.objects.create(order=order, event=event, detail=detail)


def get_bank_details() -> dict | None:
    """Static fallback bank account for buy orders when a user has neither
    enough wallet balance nor Flutterwave configured. None if not set up —
    callers should treat that as "bank transfer isn't available"."""
    name = getattr(settings, 'CRYPTO_BANK_ACCOUNT_NAME', '')
    number = getattr(settings, 'CRYPTO_BANK_ACCOUNT_NUMBER', '')
    bank = getattr(settings, 'CRYPTO_BANK_NAME', '')
    if not (name and number and bank):
        return None
    return {'account_name': name, 'account_number': number, 'bank_name': bank}


def _existing_order_for_idempotency_key(idempotency_key: str | None) -> CryptoOrder | None:
    if not idempotency_key:
        return None
    return CryptoOrder.objects.filter(idempotency_key=idempotency_key).first()


# Buy starts pending_payment (money not secured yet); sell and swap both
# require the crypto balance up front, so they start processing and only
# fall back to waiting_deposit (sell) or fail outright (swap) if reserving
# it comes up short.
_INITIAL_STATUS = {
    OrderType.BUY: OrderStatus.PENDING_PAYMENT,
    OrderType.SELL: OrderStatus.PROCESSING,
    OrderType.SWAP: OrderStatus.PROCESSING,
}


def _create_order_from_quote(user, quote, *, idempotency_key: str | None = None) -> CryptoOrder:
    for _ in range(5):
        reference = generate_order_reference()
        try:
            return CryptoOrder.objects.create(
                reference=reference,
                idempotency_key=idempotency_key or None,
                user=user,
                quote=quote,
                order_type=quote.quote_type,
                coin=quote.coin,
                to_coin=quote.to_coin,
                coin_amount=quote.coin_amount,
                to_coin_amount=quote.to_coin_amount,
                rate_ngn=quote.rate_ngn,
                to_rate_ngn=quote.to_rate_ngn,
                fee_ngn=quote.fee_ngn,
                total_ngn=quote.total_ngn,
                swap_method=quote.swap_method,
                gross_to_amount=quote.gross_to_amount,
                fee_to_coin=quote.fee_to_coin,
                status=_INITIAL_STATUS[quote.quote_type],
            )
        except IntegrityError:
            continue
    raise CryptoServiceError('Could not generate an order reference.', status=500)


def _fail_order(order: CryptoOrder, note: str, *, refund_ngn: bool) -> CryptoOrder:
    """Idempotent — a completed or already-failed order is left untouched
    (the double-execution guard: retried webhooks/requeries are safe)."""
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status in (OrderStatus.COMPLETED, OrderStatus.FAILED):
            return locked
        locked.status = OrderStatus.FAILED
        locked.note = note
        locked.save(update_fields=['status', 'note', 'updated_at'])
        if refund_ngn:
            credit_wallet(locked.user, locked.total_ngn)

    _log(order, 'order_failed', note)
    if refund_ngn:
        _log(order, 'refunded_after_failure', f'Refunded ₦{order.total_ngn} to wallet.')
    order.refresh_from_db()
    return order


def _outcome_unknown(exc: QuidaxError) -> bool:
    """No HTTP status (timeout/network) or a 5xx: Quidax may have executed
    the order anyway. A 4xx means it was definitely refused."""
    return exc.status_code is None or exc.status_code >= 500


def _flag_for_review(order: CryptoOrder, note: str) -> CryptoOrder:
    """Parks an order whose market order may or may not have executed.
    Deliberately does NOT refund NGN or release crypto: doing either when
    Quidax actually filled the order would pay the user twice."""
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status in (OrderStatus.COMPLETED, OrderStatus.FAILED):
            return locked
        locked.status = OrderStatus.PROCESSING
        locked.needs_review = True
        locked.note = note
        locked.save(update_fields=['status', 'needs_review', 'note', 'updated_at'])
    _log(order, 'quidax_outcome_unknown', f'{note} Held for admin review — check the Quidax master account.')
    order.refresh_from_db()
    return order


_plain_decimal = plain_decimal


def _buy_market_volume(order: CryptoOrder) -> str:
    """Quidax market-order volume is always in the BASE coin (btc in btcngn),
    on buys as well as sells — passing the naira amount here makes Quidax try
    to buy that many coins and fail with 'Insufficient account balance'. Only
    the crypto itself is bought; our fee stays with us as platform margin."""
    return _plain_decimal(order.coin_amount)


def _execute_quidax_buy(order: CryptoOrder, *, refund_on_fail: bool) -> CryptoOrder:
    """Places the market buy on Quidax and finalizes the order — credits the
    user's internal CryptoWallet on success. Assumes payment is already
    secured (status is payment_received going in)."""
    market = f'{order.coin}ngn'
    volume = _buy_market_volume(order)

    try:
        payload = quidax.create_instant_order(
            market=market, side='buy', volume=volume, user_id=settings.QUIDAX_USER_ID, retry=False
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Buy order request failed: {exc.message}')
        if _outcome_unknown(exc):
            return _flag_for_review(order, f'Quidax buy outcome unknown: {exc.message}')
        return _fail_order(order, f'Quidax buy failed: {exc.message}', refund_ngn=refund_on_fail)

    data = payload.get('data') or {}
    quidax_order_id = str(data.get('id') or '')
    _log(
        order,
        'quidax_buy_sent',
        f'market={market} volume={volume} quidax_order_id={quidax_order_id}',
    )
    return _complete_buy(order, quidax_order_id)


def _complete_buy(order: CryptoOrder, quidax_order_id: str) -> CryptoOrder:
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status == OrderStatus.COMPLETED:
            return locked
        locked.quidax_order_id = quidax_order_id or locked.quidax_order_id
        locked.status = OrderStatus.COMPLETED
        locked.save(update_fields=['quidax_order_id', 'status', 'updated_at'])
        credit_crypto_available(locked.user, locked.coin, locked.coin_amount)

    _log(order, 'order_completed', f'Credited {order.coin_amount} {order.coin.upper()} to wallet.')
    order.refresh_from_db()
    return order


def place_buy_order(
    user, *, quote_id, pin: str, idempotency_key: str | None = None
) -> CryptoOrder:
    """Creates the order from a locked quote, then tries each payment path
    in order: wallet balance -> Flutterwave -> bank transfer fallback.
    Wallet-funded orders execute immediately; the other two paths leave the
    order pending_payment for the client/admin to complete."""
    existing = _existing_order_for_idempotency_key(idempotency_key)
    if existing:
        return existing

    check_transaction_pin(user, pin)

    with transaction.atomic():
        quote = get_locked_quote(user, quote_id, QuoteType.BUY)
        mark_quote_used(quote)
        order = _create_order_from_quote(user, quote, idempotency_key=idempotency_key)
    _log(order, 'order_created', f'Buy {order.coin_amount} {order.coin.upper()} for ₦{order.total_ngn}')

    try:
        debit_wallet(user, order.total_ngn)
        wallet_paid = True
    except WalletServiceError:
        wallet_paid = False

    if wallet_paid:
        with transaction.atomic():
            locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
            locked.status = OrderStatus.PAYMENT_RECEIVED
            locked.save(update_fields=['status', 'updated_at'])
        _log(order, 'paid_from_wallet', f'Debited ₦{order.total_ngn} from NGN wallet.')
        order.refresh_from_db()
        return _execute_quidax_buy(order, refund_on_fail=True)

    if settings.FLW_SECRET_KEY:
        _log(order, 'awaiting_flutterwave_payment', 'Insufficient wallet balance.')
        return order

    _log(order, 'awaiting_bank_transfer', 'Insufficient wallet balance; no Flutterwave configured.')
    return order


def verify_buy_payment(user, reference: str) -> CryptoOrder:
    """POST /crypto/orders/buy/verify/ — confirms a Flutterwave charge for a
    pending_payment buy order, then executes it. On a Quidax failure here,
    the NGN is NOT refunded automatically (it already left the user's card
    via Flutterwave, not our wallet) — an admin has to sort it out."""
    try:
        order = CryptoOrder.objects.get(reference=reference, user=user, order_type=OrderType.BUY)
    except CryptoOrder.DoesNotExist:
        raise CryptoServiceError('Order not found.', code='order_not_found', status=404)

    if order.status != OrderStatus.PENDING_PAYMENT:
        # Already verified/executing/done — no-op guard against double-verification.
        return order

    try:
        payload = flutterwave.verify_transaction(order.reference)
    except FlutterwaveError as exc:
        raise CryptoServiceError(
            f'Could not verify payment: {exc.message}', code='flw_unreachable', status=502
        )

    data = payload.get('data') or {}
    flw_ok = payload.get('status') == 'success' and data.get('status') == 'successful'
    paid_amount = Decimal(str(data.get('amount', 0)))

    if not flw_ok or paid_amount < order.total_ngn:
        raise CryptoServiceError('Payment could not be verified.', code='verification_failed')

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status != OrderStatus.PENDING_PAYMENT:
            return locked
        locked.status = OrderStatus.PAYMENT_RECEIVED
        locked.flw_tx_ref = str(data.get('id') or order.reference)
        locked.save(update_fields=['status', 'flw_tx_ref', 'updated_at'])

    _log(order, 'paid_via_flutterwave', f'Verified ₦{paid_amount} via Flutterwave.')
    order.refresh_from_db()
    return _execute_quidax_buy(order, refund_on_fail=False)


def submit_payment_proof(user, reference: str, proof_file) -> CryptoOrder:
    """POST /crypto/orders/<ref>/proof/ — bank-transfer path C. Doesn't
    execute anything itself; an admin reviews the proof and approves
    (phase 9), which is what actually triggers the Quidax buy."""
    try:
        order = CryptoOrder.objects.get(reference=reference, user=user, order_type=OrderType.BUY)
    except CryptoOrder.DoesNotExist:
        raise CryptoServiceError('Order not found.', code='order_not_found', status=404)

    if order.status != OrderStatus.PENDING_PAYMENT:
        raise CryptoServiceError(
            'This order is not awaiting payment proof.', code='invalid_state'
        )

    order.payment_proof = proof_file
    order.save(update_fields=['payment_proof', 'updated_at'])
    _log(order, 'proof_uploaded', 'Payment proof uploaded; awaiting admin review.')
    return order


# ─── Sell ───────────────────────────────────────────────────────────────────


def _sell_volume_crypto(order: CryptoOrder) -> str:
    """Crypto quantity as a plain decimal string."""
    return _plain_decimal(order.coin_amount)


def send_master_sell(order: CryptoOrder, *, retry: bool = True) -> str:
    """Places the market sell on the master Quidax account and returns the
    Quidax order id. Raises QuidaxError; touches no balances — callers
    decide what a failure means for the reservation."""
    market = f'{order.coin}ngn'
    volume = _sell_volume_crypto(order)
    payload = quidax.create_instant_order(
        market=market, side='sell', volume=volume, user_id=settings.QUIDAX_USER_ID, retry=retry
    )
    data = payload.get('data') or {}
    quidax_order_id = str(data.get('id') or '')
    _log(
        order,
        'quidax_sell_sent',
        f'market={market} volume={volume} quidax_order_id={quidax_order_id}',
    )
    return quidax_order_id


def _execute_quidax_sell(order: CryptoOrder) -> CryptoOrder:
    """Places the market sell on Quidax and finalizes — on success, debits
    the reserved crypto and credits the NGN payout; on failure, releases
    the reservation back to available (nothing external moved yet, so
    there's nothing to refund on the NGN side)."""
    try:
        quidax_order_id = send_master_sell(order, retry=False)
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Sell order request failed: {exc.message}')
        if _outcome_unknown(exc):
            # Crypto stays reserved until an admin confirms either way.
            return _flag_for_review(order, f'Quidax sell outcome unknown: {exc.message}')
        release_reserved_crypto(order.user, order.coin, order.coin_amount)
        _log(order, 'reservation_released', f'Released {order.coin_amount} {order.coin.upper()} back to available.')
        return _fail_order(order, f'Quidax sell failed: {exc.message}', refund_ngn=False)

    return complete_sell(order, quidax_order_id)


def complete_sell(order: CryptoOrder, quidax_order_id: str) -> CryptoOrder:
    """Finalizes a sell Quidax accepted: debits the reserved crypto and
    credits total_ngn, exactly once (a completed order is left alone)."""
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status == OrderStatus.COMPLETED:
            return locked
        locked.quidax_order_id = quidax_order_id or locked.quidax_order_id
        locked.status = OrderStatus.COMPLETED
        locked.save(update_fields=['quidax_order_id', 'status', 'updated_at'])
        debit_reserved_crypto(locked.user, locked.coin, locked.coin_amount)
        credit_wallet(locked.user, locked.total_ngn)

    _log(order, 'order_completed', f'Credited ₦{order.total_ngn} payout to wallet.')
    order.refresh_from_db()
    return order


def _start_processing(order: CryptoOrder) -> CryptoOrder:
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        locked.status = OrderStatus.PROCESSING
        locked.save(update_fields=['status', 'updated_at'])
    order.refresh_from_db()
    return order


def place_sell_order(
    user, *, quote_id, pin: str, idempotency_key: str | None = None
) -> CryptoOrder:
    """Reserves the crypto and sells immediately if the user has enough
    balance. Otherwise the order waits in waiting_deposit — the deposit
    address to fund it comes from crypto/deposits.py (a later phase); for
    now this just parks the order until the user has enough to retry.
    """
    existing = _existing_order_for_idempotency_key(idempotency_key)
    if existing:
        return existing

    check_transaction_pin(user, pin)

    with transaction.atomic():
        quote = get_locked_quote(user, quote_id, QuoteType.SELL)
        mark_quote_used(quote)
        order = _create_order_from_quote(user, quote, idempotency_key=idempotency_key)
    _log(order, 'order_created', f'Sell {order.coin_amount} {order.coin.upper()} for ~₦{order.total_ngn}')

    try:
        reserve_crypto(user, order.coin, order.coin_amount)
    except CryptoServiceError:
        with transaction.atomic():
            locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
            locked.status = OrderStatus.WAITING_DEPOSIT
            locked.save(update_fields=['status', 'updated_at'])
        _log(
            order,
            'awaiting_deposit',
            'Insufficient crypto balance; waiting for a deposit before this can be sold.',
        )
        order.refresh_from_db()
        return order

    _log(order, 'reserved_balance', f'Reserved {order.coin_amount} {order.coin.upper()}.')
    return _execute_quidax_sell(order)


def _retry_sell_order(order: CryptoOrder) -> CryptoOrder:
    if order.status != OrderStatus.WAITING_DEPOSIT:
        return order

    reserve_crypto(order.user, order.coin, order.coin_amount)  # raises if still insufficient

    _log(order, 'reserved_balance', f'Reserved {order.coin_amount} {order.coin.upper()} after deposit.')
    order = _start_processing(order)
    return _execute_quidax_sell(order)


def retry_sell_after_deposit(user, reference: str) -> CryptoOrder:
    """Re-attempts a waiting_deposit sell — called after an on-chain deposit
    lands (phase 7's webhook) or when the user manually retries from the app."""
    try:
        order = CryptoOrder.objects.get(reference=reference, user=user, order_type=OrderType.SELL)
    except CryptoOrder.DoesNotExist:
        raise CryptoServiceError('Order not found.', code='order_not_found', status=404)
    return _retry_sell_order(order)


# ─── Swap ───────────────────────────────────────────────────────────────────


def _is_precision_error(exc: QuidaxError) -> bool:
    return not _outcome_unknown(exc) and 'precision' in (exc.message or '').lower()


def _market_buy_fitting_precision(market: str, amount: Decimal) -> tuple[dict, Decimal]:
    """Market buy of `amount` base coin. Each market caps how many decimals
    its volume may have and Quidax doesn't tell us the cap up front, so on a
    'precision exceeds maximum limit' refusal (a 4xx — nothing executed) the
    amount is rounded DOWN one decimal at a time and retried. Returns the
    payload and the volume that actually went through."""
    decimals = max(-amount.normalize().as_tuple().exponent, 0)
    volume = amount
    while True:
        try:
            payload = quidax.create_instant_order(
                market=market, side='buy', volume=_plain_decimal(volume),
                user_id=settings.QUIDAX_USER_ID, retry=False,
            )
            return payload, volume
        except QuidaxError as exc:
            if not _is_precision_error(exc) or decimals == 0:
                raise
            decimals -= 1
            volume = amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
            if volume <= 0:
                raise


def _execute_quidax_swap(order: CryptoOrder) -> CryptoOrder:
    """Two Quidax market orders back to back: sell the source coin, then buy
    the destination coin with the proceeds. If leg 1 fails, nothing moved —
    release the reservation like a normal failed sell. If leg 2 fails AFTER
    leg 1 succeeded, the source coin is already gone on Quidax's side, so
    releasing it back to available would create a phantom balance; instead
    it's committed (debited) and flagged for admin review — same principle
    as never auto-refunding a Flutterwave-funded failed buy.
    """
    from_market = f'{order.coin}ngn'
    sell_volume = _sell_volume_crypto(order)

    try:
        sell_payload = quidax.create_instant_order(
            market=from_market, side='sell', volume=sell_volume, user_id=settings.QUIDAX_USER_ID, retry=False
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Swap sell leg ({order.coin.upper()}) failed: {exc.message}')
        if _outcome_unknown(exc):
            # Source coin stays reserved; nothing bought yet.
            return _flag_for_review(order, f'Swap sell leg outcome unknown: {exc.message}')
        release_reserved_crypto(order.user, order.coin, order.coin_amount)
        _log(
            order,
            'reservation_released',
            f'Released {order.coin_amount} {order.coin.upper()} back to available.',
        )
        return _fail_order(
            order,
            f"We couldn't complete your swap right now. Your {order.coin.upper()} was not used "
            'and is back in your balance. Please try again later.',
            refund_ngn=False,
        )

    sell_data = sell_payload.get('data') or {}
    sell_order_id = str(sell_data.get('id') or '')
    _log(
        order,
        'quidax_sell_leg_sent',
        f'market={from_market} volume={sell_volume} quidax_order_id={sell_order_id}',
    )
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        locked.quidax_sell_order_id = sell_order_id or locked.quidax_sell_order_id
        locked.save(update_fields=['quidax_sell_order_id', 'updated_at'])
    order.refresh_from_db()
    return _execute_swap_buy_leg(order)


def _swap_sell_leg_done(order: CryptoOrder) -> bool:
    return bool(order.quidax_sell_order_id)


def _execute_swap_buy_leg(order: CryptoOrder) -> CryptoOrder:
    """Leg 2. Only runs once leg 1 is known to have executed, so from here
    on the source coin is gone on Quidax: it's debited on any outcome, never
    released."""
    to_market = f'{order.to_coin}ngn'
    try:
        buy_payload, bought = _market_buy_fitting_precision(to_market, order.to_coin_amount)
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Swap buy leg ({order.to_coin.upper()}) failed: {exc.message}')
        debit_reserved_crypto(order.user, order.coin, order.coin_amount)
        if _outcome_unknown(exc):
            _log(
                order,
                'reservation_debited',
                f'{order.coin_amount} {order.coin.upper()} already sold on the sell leg.',
            )
            return _flag_for_review(order, f'Swap buy leg outcome unknown: {exc.message}')
        return _refund_failed_swap_as_ngn(order, exc.message)

    buy_data = buy_payload.get('data') or {}
    buy_order_id = str(buy_data.get('id') or '')
    _log(
        order,
        'quidax_buy_leg_sent',
        f'market={to_market} volume={_plain_decimal(bought)} quidax_order_id={buy_order_id}',
    )
    return _complete_swap(order, buy_order_id, debit_source=True, bought=bought)


def _swap_net_ngn(order: CryptoOrder) -> Decimal:
    """What the sold source coin is worth to the user after our fee — total_ngn
    is the swap's notional; the fee comes out of the destination side."""
    return (order.total_ngn - order.fee_ngn).quantize(Decimal('0.01'), rounding=ROUND_DOWN)


def _refund_failed_swap_as_ngn(order: CryptoOrder, reason: str) -> CryptoOrder:
    """Buy leg definitely didn't execute, but the sell leg did — the naira
    from that sale is sitting in the master account. Pay it (net of fee) into
    the user's NGN wallet so they're never left with nothing while waiting
    for an admin. The source coin must already be debited."""
    net_ngn = _swap_net_ngn(order)
    note = (
        f"We couldn't buy {order.to_coin.upper()} right now, so the value of your "
        f'{order.coin.upper()} (₦{net_ngn}) was added to your NGN wallet instead.'
    )
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status in (OrderStatus.COMPLETED, OrderStatus.FAILED):
            return locked
        locked.status = OrderStatus.FAILED
        locked.note = note
        locked.save(update_fields=['status', 'note', 'updated_at'])
        credit_wallet(locked.user, net_ngn)
        _log(locked, 'swap_refunded_ngn', f'Buy leg failed: {reason}. Credited ₦{net_ngn} to NGN wallet.')

    _log(order, 'order_failed', note)
    order.refresh_from_db()
    return order


def _complete_swap(
    order: CryptoOrder, buy_order_id: str, *, debit_source: bool, bought: Decimal | None = None
) -> CryptoOrder:
    """debit_source=False when the source coin was already debited (a buy
    leg that was parked for review, then confirmed executed). bought is the
    destination amount Quidax actually filled when precision forced it below
    the quote; the shortfall's NGN value goes to the user's wallet."""
    dust_ngn = Decimal('0')
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status == OrderStatus.COMPLETED:
            return locked
        update_fields = ['quidax_order_id', 'status', 'updated_at']
        if bought is not None and bought < locked.to_coin_amount:
            dust_ngn = ((locked.to_coin_amount - bought) * locked.to_rate_ngn).quantize(
                Decimal('0.01'), rounding=ROUND_DOWN
            )
            locked.to_coin_amount = bought
            update_fields.append('to_coin_amount')
        locked.quidax_order_id = buy_order_id or locked.quidax_order_id
        locked.status = OrderStatus.COMPLETED
        locked.save(update_fields=update_fields)
        if debit_source:
            debit_reserved_crypto(locked.user, locked.coin, locked.coin_amount)
        credit_crypto_available(locked.user, locked.to_coin, locked.to_coin_amount)
        if dust_ngn > 0:
            credit_wallet(locked.user, dust_ngn)

    order.refresh_from_db()
    _log(order, 'order_completed', f'Credited {order.to_coin_amount} {order.to_coin.upper()} to wallet.')
    if dust_ngn > 0:
        _log(order, 'precision_remainder_refunded', f'Credited ₦{dust_ngn} (amount below market precision) to NGN wallet.')
    return order


def place_swap_order(
    user, *, quote_id, pin: str, idempotency_key: str | None = None
) -> CryptoOrder:
    """Swap requires the full source-coin balance up front — no
    waiting_deposit fallback like sell; if reserving comes up short, the
    order just fails immediately."""
    existing = _existing_order_for_idempotency_key(idempotency_key)
    if existing:
        return existing

    check_transaction_pin(user, pin)

    with transaction.atomic():
        quote = get_locked_quote(user, quote_id, QuoteType.SWAP)
        mark_quote_used(quote)
        order = _create_order_from_quote(user, quote, idempotency_key=idempotency_key)
    _log(
        order,
        'order_created',
        f'Swap {order.coin_amount} {order.coin.upper()} -> {order.to_coin.upper()}',
    )

    try:
        reserve_crypto(user, order.coin, order.coin_amount)
    except CryptoServiceError:
        return _fail_order(
            order, f"You don't have enough {order.coin.upper()} for this swap.", refund_ngn=False
        )

    _log(order, 'reserved_balance', f'Reserved {order.coin_amount} {order.coin.upper()}.')
    if order.swap_method == SwapMethod.INSTANT:
        return _execute_instant_swap(order)
    return _execute_quidax_swap(order)


# ─── Instant swap (Quidax swap_quotation -> confirm) ───────────────────────
#
# One quoted conversion on the master account instead of two market orders.
# Lifecycle, all keyed on the order row lock so every path is idempotent:
#   reserve source -> fresh quotation (moves nothing) -> slippage check ->
#   save quotation id -> confirm -> save swap id -> wait briefly for
#   'completed' | 'failed'. Anything else leaves the order processing for
#   the swap_transaction.* webhook or reconcile_instant_swap to finish.
# Source funds are only released when Quidax definitely did not convert;
# a timeout/5xx on confirm parks the order (needs_review) with the source
# still reserved.

_SWAP_COMPLETED = 'completed'
_SWAP_FAILED = 'failed'


def _quidax_data(payload) -> dict:
    data = payload.get('data') if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _not_used_note(order: CryptoOrder, reason: str) -> str:
    return f'{reason} Your {order.coin.upper()} was not used and is back in your balance.'


def _release_instant_swap(order: CryptoOrder, note: str, detail: str) -> CryptoOrder:
    """Quidax definitely did not convert: give the reserved source back and
    fail the order — once, however many times this is reached."""
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status in (OrderStatus.COMPLETED, OrderStatus.FAILED):
            return locked
        release_reserved_crypto(locked.user, locked.coin, locked.coin_amount)
        locked.status = OrderStatus.FAILED
        locked.needs_review = False
        locked.note = note
        locked.save(update_fields=['status', 'needs_review', 'note', 'updated_at'])
        _log(locked, 'reservation_released', f'Released {locked.coin_amount} {locked.coin.upper()}. {detail}'.strip())
        _log(locked, 'order_failed', note)
    order.refresh_from_db()
    return order


def _finalize_instant_swap(order: CryptoOrder, swap: dict) -> CryptoOrder:
    """Quidax reports the conversion completed: debit the reserved source and
    credit what Quidax actually delivered minus our fee — exactly once. A
    report we can't trust (wrong coin, no amount) is parked for an admin
    rather than credited."""
    received = quidax_decimal(swap.get('received_amount'))
    to_currency = str(swap.get('to_currency') or order.to_coin).lower()
    from_currency = str(swap.get('from_currency') or order.coin).lower()
    from_amount = quidax_decimal(swap.get('from_amount'))
    if (
        received is None
        or received <= 0
        or to_currency != order.to_coin
        or from_currency != order.coin
        or (from_amount is not None and from_amount != order.coin_amount)
    ):
        return _flag_for_review(
            order, f'Quidax reported swap {swap.get("id")} completed with unusable data: {swap!r:.300}'
        )

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status in (OrderStatus.COMPLETED, OrderStatus.FAILED):
            return locked
        fee = locked.fee_to_coin or Decimal('0')
        credit = max((received - fee).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN), Decimal('0'))
        debit_reserved_crypto(locked.user, locked.coin, locked.coin_amount)
        if credit > 0:
            credit_crypto_available(locked.user, locked.to_coin, credit)
        locked.to_coin_amount = credit
        locked.quidax_swap_id = str(swap.get('id') or locked.quidax_swap_id)
        locked.quidax_order_id = locked.quidax_swap_id
        locked.status = OrderStatus.COMPLETED
        locked.needs_review = False
        locked.note = ''
        locked.save(
            update_fields=[
                'to_coin_amount', 'quidax_swap_id', 'quidax_order_id', 'status', 'needs_review', 'note', 'updated_at',
            ]
        )
        _log(
            locked,
            'order_completed',
            f'Quidax delivered {received} {locked.to_coin.upper()} (execution_price='
            f'{swap.get("execution_price")}); fee {fee}; credited {credit} {locked.to_coin.upper()}.',
        )
    order.refresh_from_db()
    return order


def _apply_swap_result(order: CryptoOrder, swap: dict) -> CryptoOrder:
    """Routes a Quidax swap transaction (from confirm, polling, webhook or
    reconciliation) to the matching outcome. Non-terminal statuses leave the
    order processing."""
    status = str(swap.get('status') or '').lower()
    if status == _SWAP_COMPLETED:
        return _finalize_instant_swap(order, swap)
    if status == _SWAP_FAILED:
        return _release_instant_swap(
            order,
            _not_used_note(order, "We couldn't complete your swap."),
            f'Quidax swap {swap.get("id")} failed.',
        )
    _log(order, 'swap_pending', f'Quidax swap {swap.get("id")} status={status or "unknown"}.')
    order.refresh_from_db()
    return order


def _await_swap(swap: dict) -> dict:
    """Polls GET swap_transactions/{id} for a few seconds so the common case
    answers the user immediately; gives up quietly (the webhook or
    reconciler takes over) rather than holding the request open."""
    deadline = time.monotonic() + settings.QUIDAX_INSTANT_SWAP_WAIT_SECONDS
    while str(swap.get('status') or '').lower() not in (_SWAP_COMPLETED, _SWAP_FAILED):
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
        try:
            fresh = _quidax_data(quidax.get_swap_transaction(str(swap['id']), user_id=settings.QUIDAX_USER_ID))
        except QuidaxError:
            break
        if fresh:
            swap = fresh
    return swap


def _execute_instant_swap(order: CryptoOrder) -> CryptoOrder:
    """Runs after the source is reserved; see the section comment above."""
    try:
        quotation = _quidax_data(
            quidax.create_swap_quotation(
                from_currency=order.coin,
                to_currency=order.to_coin,
                from_amount=plain_decimal(order.coin_amount),
                user_id=settings.QUIDAX_USER_ID,
            )
        )
    except QuidaxError as exc:
        # A quotation moves nothing — whatever went wrong, it's safe to release.
        _log(order, 'quidax_error', f'Swap quotation failed: {exc.status_code} {exc.message}')
        return _release_instant_swap(
            order, _not_used_note(order, "We couldn't get a swap price right now. Please try again."), ''
        )

    quotation_id = str(quotation.get('id') or '')
    gross = quidax_decimal(quotation.get('to_amount'))
    if (
        not quotation_id
        or gross is None
        or gross <= 0
        or str(quotation.get('from_currency', order.coin)).lower() != order.coin
        or str(quotation.get('to_currency', order.to_coin)).lower() != order.to_coin
    ):
        _log(order, 'quidax_error', f'Unusable swap quotation: {quotation!r:.300}')
        return _release_instant_swap(
            order, _not_used_note(order, "We couldn't get a swap price right now. Please try again."), ''
        )

    slippage = Decimal(str(settings.CRYPTO_SWAP_MAX_SLIPPAGE_PERCENT)) / Decimal('100')
    floor = order.gross_to_amount * (Decimal('1') - slippage)
    if gross < floor:
        _log(
            order,
            'price_changed',
            f'Quidax now quotes {gross} {order.to_coin.upper()}, below the confirmed '
            f'{order.gross_to_amount} less {settings.CRYPTO_SWAP_MAX_SLIPPAGE_PERCENT}% ({floor}).',
        )
        return _release_instant_swap(
            order,
            _not_used_note(order, 'The price moved before your swap went through. Please get a new quote.'),
            '',
        )

    # Persist the quotation id BEFORE confirming: if anything dies after the
    # confirm call, reconciliation can still find the swap by this id.
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        locked.quidax_swap_quotation_id = quotation_id
        locked.save(update_fields=['quidax_swap_quotation_id', 'updated_at'])
    order.refresh_from_db()
    _log(
        order,
        'quidax_swap_quoted',
        f'quotation={quotation_id} to_amount={gross} quoted_price={quotation.get("quoted_price")} '
        f'expires_at={quotation.get("expires_at")}',
    )

    try:
        swap = _quidax_data(
            quidax.confirm_swap_quotation(quotation_id, user_id=settings.QUIDAX_USER_ID)
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Swap confirm failed: {exc.status_code} {exc.message}')
        if _outcome_unknown(exc):
            return _flag_for_review(order, f'Instant swap confirm outcome unknown: {exc.message}')
        # 4xx (quotation used/expired, insufficient balance, ...): nothing converted.
        return _release_instant_swap(
            order, _not_used_note(order, "We couldn't complete your swap right now. Please try again later."), ''
        )

    swap_id = str(swap.get('id') or '')
    if not swap_id:
        return _flag_for_review(order, f'Quidax accepted the swap confirm but returned no swap id: {swap!r:.300}')
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        locked.quidax_swap_id = swap_id
        locked.save(update_fields=['quidax_swap_id', 'updated_at'])
    order.refresh_from_db()
    _log(order, 'quidax_swap_confirmed', f'swap={swap_id} status={swap.get("status")}')

    swap = _await_swap(swap)
    try:
        return _apply_swap_result(order, swap)
    except DatabaseError:
        # Quidax has the outcome; our write failed. The order keeps its swap
        # id and stays processing, so the webhook or reconciler finishes it.
        logger.exception('Finalizing instant swap %s failed; left for reconciliation.', order.reference)
        order.refresh_from_db()
        return order


def _find_instant_swap_order(swap: dict) -> CryptoOrder | None:
    swap_id = str(swap.get('id') or '')
    quotation_id = str((swap.get('swap_quotation') or {}).get('id') or '')
    qs = CryptoOrder.objects.filter(order_type=OrderType.SWAP, swap_method=SwapMethod.INSTANT)
    if swap_id:
        order = qs.filter(quidax_swap_id=swap_id).first()
        if order:
            return order
    if quotation_id:
        return qs.filter(quidax_swap_quotation_id=quotation_id).first()
    return None


def handle_swap_webhook(payload: dict) -> None:
    """swap_transaction.completed / swap_transaction.failed. Safe to receive
    any number of times: finalizing is a no-op once the order is closed."""
    swap = _quidax_data(payload)
    event = str(payload.get('event') or '')
    order = _find_instant_swap_order(swap)
    if order is None:
        logger.info('Quidax %s for unknown swap %s ignored.', event, swap.get('id'))
        return
    if order.status != OrderStatus.PROCESSING:
        return
    if not swap.get('status'):
        swap = {**swap, 'status': _SWAP_FAILED if event.endswith('failed') else _SWAP_COMPLETED}
    _log(order, 'quidax_webhook_received', f'{event} for swap {swap.get("id")}.')
    if swap.get('id') and not order.quidax_swap_id:
        CryptoOrder.objects.filter(pk=order.pk).update(quidax_swap_id=str(swap['id']))
        order.refresh_from_db()
    _apply_swap_result(order, swap)


def reconcile_instant_swap(order: CryptoOrder) -> CryptoOrder:
    """Asks Quidax what became of a processing instant swap and applies it.
    Never releases or credits on a guess: if Quidax has no record we can tie
    to this order, it stays as it is for an admin."""
    if order.status != OrderStatus.PROCESSING or order.swap_method != SwapMethod.INSTANT:
        return order

    if order.quidax_swap_id:
        try:
            swap = _quidax_data(
                quidax.get_swap_transaction(order.quidax_swap_id, user_id=settings.QUIDAX_USER_ID)
            )
        except QuidaxError as exc:
            _log(order, 'reconcile_failed', f'Could not fetch swap {order.quidax_swap_id}: {exc.message}')
            return order
    elif order.quidax_swap_quotation_id:
        try:
            rows = quidax.list_swap_transactions(user_id=settings.QUIDAX_USER_ID)
        except QuidaxError as exc:
            _log(order, 'reconcile_failed', f'Could not list swaps: {exc.message}')
            return order
        swap = next(
            (
                r for r in rows
                if isinstance(r, dict)
                and str((r.get('swap_quotation') or {}).get('id') or '') == order.quidax_swap_quotation_id
            ),
            None,
        )
        if swap is None:
            _log(
                order,
                'reconcile_not_found',
                f'No Quidax swap for quotation {order.quidax_swap_quotation_id} yet — left for review.',
            )
            return order
        CryptoOrder.objects.filter(pk=order.pk).update(quidax_swap_id=str(swap.get('id') or ''))
        order.refresh_from_db()
    else:
        # Reserved but never got as far as a quotation, so confirm was never sent.
        return _release_instant_swap(
            order, _not_used_note(order, "We couldn't complete your swap."), 'No quotation was ever created.'
        )

    return _apply_swap_result(order, swap)


def reconcile_due_instant_swaps(min_age_seconds: int = 60) -> list[tuple[CryptoOrder, str]]:
    """Every processing instant swap older than min_age_seconds, reconciled
    against Quidax. Returns (order, status before) pairs. Run by the
    settlement worker loop and the reconcile_instant_swaps command."""
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(seconds=min_age_seconds)
    pending = CryptoOrder.objects.filter(
        order_type=OrderType.SWAP,
        swap_method=SwapMethod.INSTANT,
        status=OrderStatus.PROCESSING,
        created_at__lte=cutoff,
    ).order_by('created_at')
    results = []
    for order in pending:
        before = order.status
        try:
            results.append((reconcile_instant_swap(order), before))
        except Exception:  # one bad order must not stop the rest
            logger.exception('Reconciling instant swap %s failed.', order.reference)
    return results


def admin_check_instant_swap(order: CryptoOrder, note: str = '') -> CryptoOrder:
    """Admin "Check Quidax" for a processing instant swap — same as the
    reconciler, on demand."""
    if order.swap_method != SwapMethod.INSTANT or order.status != OrderStatus.PROCESSING:
        raise CryptoServiceError('Only processing instant swaps can be checked.', code='invalid_state')
    if note:
        _log(order, 'admin_check', note)
    return reconcile_instant_swap(order)


def list_orders(user):
    return CryptoOrder.objects.filter(user=user).order_by('-created_at')[:50]


# ─── Admin operations (ops safety net) ─────────────────────────────────────
#
# No customer-facing endpoint calls these — they're the manual recovery
# path for orders that got stuck because a payment provider or Quidax
# needed a human to look at them.


def admin_approve_buy(order: CryptoOrder, note: str = '') -> CryptoOrder:
    """Bank-transfer buy with proof uploaded — admin visually confirmed the
    transfer, so this behaves like verify_buy_payment: real external money
    already moved, so a Quidax failure here does NOT auto-refund."""
    if order.order_type != OrderType.BUY or order.status != OrderStatus.PENDING_PAYMENT:
        raise CryptoServiceError(
            'Only pending-payment buy orders can be approved.', code='invalid_state'
        )

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status != OrderStatus.PENDING_PAYMENT:
            return locked
        locked.status = OrderStatus.PAYMENT_RECEIVED
        locked.save(update_fields=['status', 'updated_at'])

    _log(order, 'admin_approved_payment', f'Admin confirmed payment received. {note}'.strip())
    order.refresh_from_db()
    return _execute_quidax_buy(order, refund_on_fail=False)


def admin_reject_order(order: CryptoOrder, note: str = '') -> CryptoOrder:
    """Cancels a stuck pending_payment buy or waiting_deposit sell. Neither
    state has committed any of the user's balance yet (wallet-funded buys
    execute immediately; sell only reserves once it actually has the
    balance), so there's nothing to refund — just close it out."""
    if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.WAITING_DEPOSIT):
        raise CryptoServiceError(
            'Only pending-payment or waiting-deposit orders can be rejected.', code='invalid_state'
        )

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.WAITING_DEPOSIT):
            return locked
        locked.status = OrderStatus.FAILED
        locked.note = note or 'Rejected by admin.'
        locked.save(update_fields=['status', 'note', 'updated_at'])

    _log(order, 'admin_rejected', note or 'Rejected by admin.')
    order.refresh_from_db()
    return order


def admin_confirm_sell_deposit(order: CryptoOrder, note: str = '') -> CryptoOrder:
    """Same effect as the user hitting retry — for when the deposit webhook
    was missed and an admin needs to nudge it along manually."""
    if order.order_type != OrderType.SELL or order.status != OrderStatus.WAITING_DEPOSIT:
        raise CryptoServiceError(
            'Only waiting-deposit sell orders can be confirmed.', code='invalid_state'
        )
    if note:
        _log(order, 'admin_confirmed_deposit', note)
    return _retry_sell_order(order)


def admin_retry_buy(order: CryptoOrder, note: str = '') -> CryptoOrder:
    """Retries a failed buy. Only re-debits the wallet if the earlier
    failure actually refunded it (wallet-funded path) — a Flutterwave- or
    bank-funded buy that failed was never refunded in the first place, so
    retrying it must NOT touch the wallet again."""
    if order.order_type != OrderType.BUY or order.status != OrderStatus.FAILED:
        raise CryptoServiceError('Only failed buy orders can be retried.', code='invalid_state')

    was_refunded = order.logs.filter(event='refunded_after_failure').exists()

    if was_refunded:
        try:
            debit_wallet(order.user, order.total_ngn)
        except WalletServiceError as exc:
            raise CryptoServiceError(exc.message, code=exc.code, status=exc.status)
        _log(
            order,
            'admin_retry_redebited',
            f'Re-debited ₦{order.total_ngn} from wallet for retry. {note}'.strip(),
        )
    else:
        _log(
            order,
            'admin_retry_no_redebit',
            f'Retrying without re-debiting — original payment was never refunded. {note}'.strip(),
        )

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        locked.status = OrderStatus.PAYMENT_RECEIVED
        locked.note = ''
        locked.save(update_fields=['status', 'note', 'updated_at'])
    order.refresh_from_db()

    return _execute_quidax_buy(order, refund_on_fail=was_refunded)


def _wallet_payment_outstanding(order: CryptoOrder) -> bool:
    """True if the NGN for the current buy attempt came out of our wallet
    and hasn't been refunded since — Flutterwave/bank-funded buys never are."""
    events = list(order.logs.values_list('event', flat=True))
    paid = events.count('paid_from_wallet') + events.count('admin_retry_redebited')
    return paid > events.count('refunded_after_failure')


def admin_resolve_unknown_order(order: CryptoOrder, note: str = '', *, executed: bool) -> CryptoOrder:
    """Closes out an order parked by _flag_for_review, once an admin has
    checked the Quidax master account's order history. executed=True
    finishes it as if Quidax had answered success; executed=False as if it
    had refused (refund/release exactly as a normal failure would)."""
    if not order.needs_review or order.status != OrderStatus.PROCESSING:
        raise CryptoServiceError(
            'Only orders held for review can be resolved this way.', code='invalid_state'
        )
    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if not locked.needs_review or locked.status != OrderStatus.PROCESSING:
            return locked
        locked.needs_review = False
        locked.save(update_fields=['needs_review', 'updated_at'])
    order.refresh_from_db()
    outcome = 'executed' if executed else 'did not execute'
    _log(order, 'admin_resolved', f'Admin confirmed the Quidax order {outcome}. {note}'.strip())

    if order.order_type == OrderType.BUY:
        if executed:
            return _complete_buy(order, '')
        return _fail_order(
            order,
            'Quidax buy did not execute (confirmed by admin).',
            refund_ngn=_wallet_payment_outstanding(order),
        )

    if order.order_type == OrderType.SELL:
        if executed:
            return complete_sell(order, '')
        release_reserved_crypto(order.user, order.coin, order.coin_amount)
        _log(order, 'reservation_released', f'Released {order.coin_amount} {order.coin.upper()} back to available.')
        return _fail_order(order, 'Quidax sell did not execute (confirmed by admin).', refund_ngn=False)

    if order.swap_method == SwapMethod.INSTANT:
        # Quidax's own record beats the admin's reading of it.
        order = reconcile_instant_swap(order)
        if order.status != OrderStatus.PROCESSING:
            return order
        if executed:
            # No Quidax figure to go on: credit what the confirmed quotation promised.
            return _finalize_instant_swap(
                order,
                {
                    'id': order.quidax_swap_id or 'admin-confirmed',
                    'status': _SWAP_COMPLETED,
                    'received_amount': str(order.gross_to_amount),
                },
            )
        return _release_instant_swap(
            order, _not_used_note(order, "We couldn't complete your swap."), 'Admin confirmed it did not execute.'
        )

    # Swap: which leg was unknown decides what's still held.
    if not _swap_sell_leg_done(order):
        if executed:
            order.quidax_sell_order_id = 'admin-confirmed'
            order.save(update_fields=['quidax_sell_order_id', 'updated_at'])
            return _execute_swap_buy_leg(order)
        release_reserved_crypto(order.user, order.coin, order.coin_amount)
        _log(order, 'reservation_released', f'Released {order.coin_amount} {order.coin.upper()} back to available.')
        return _fail_order(order, 'Swap sell leg did not execute (confirmed by admin).', refund_ngn=False)

    if executed:
        return _complete_swap(order, '', debit_source=False)
    return _refund_failed_swap_as_ngn(order, 'buy leg did not execute, confirmed by admin')


def admin_resolve_failed_swap(order: CryptoOrder, note: str = '', *, credit: str) -> CryptoOrder:
    """Compensates a swap whose sell leg executed but whose buy leg failed —
    the source coin is gone and nothing was credited. credit='to_coin' BUYS
    the destination coin on the Quidax master account and credits the user
    only once that buy fills, so the balance is always backed by real coin;
    credit='ngn' pays the swap's net NGN value (notional - fee) into their
    wallet — the NGN from the sell leg is already in the master account.
    Runs at most once per order."""
    if order.order_type != OrderType.SWAP or order.status != OrderStatus.FAILED:
        raise CryptoServiceError('Only failed swap orders can be resolved this way.', code='invalid_state')
    if credit not in ('to_coin', 'ngn'):
        raise CryptoServiceError('Invalid credit type.', code='invalid_credit')

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        events = list(locked.logs.values_list('event', flat=True))
        if {'admin_swap_compensated', 'swap_refunded_ngn'} & set(events):
            raise CryptoServiceError('This swap has already been compensated.', code='already_resolved')
        if 'reservation_debited_needs_review' not in events:
            raise CryptoServiceError(
                'This swap never sold its source coin — nothing to compensate.', code='invalid_state'
            )
        if events.count('admin_swap_buy_started') > events.count('admin_swap_buy_failed'):
            # A Credit coin buy is in flight, or its outcome is unknown —
            # paying out anything else now could pay the user twice.
            raise CryptoServiceError(
                'A Quidax buy for this swap is in progress or its outcome is unknown. Check the '
                'Quidax master account order history before doing anything else.',
                code='buy_pending',
            )

        if credit == 'ngn':
            net_ngn = _swap_net_ngn(locked)
            credit_wallet(locked.user, net_ngn)
            locked.note = f'Compensated with ₦{net_ngn} after the buy leg failed.'
            locked.save(update_fields=['note', 'updated_at'])
            _log(locked, 'admin_swap_compensated', f'Credited ₦{net_ngn} to NGN wallet. {note}'.strip())
            order.refresh_from_db()
            return order

        # Claim the buy before calling Quidax so a double click can't buy twice.
        _log(
            locked,
            'admin_swap_buy_started',
            f'Buying {locked.to_coin_amount} {locked.to_coin.upper()} on Quidax. {note}'.strip(),
        )

    to_market = f'{order.to_coin}ngn'
    try:
        payload, bought = _market_buy_fitting_precision(to_market, order.to_coin_amount)
    except QuidaxError as exc:
        if _outcome_unknown(exc):
            _log(order, 'admin_swap_buy_unknown', f'Quidax did not answer: {exc.message}')
            raise CryptoServiceError(
                f'Quidax did not answer ({exc.message}). Check the master account order history: if '
                f'{order.to_coin.upper()} was bought, credit it by hand; nothing was credited here.',
                code='outcome_unknown',
                status=502,
            )
        _log(order, 'admin_swap_buy_failed', exc.message)
        raise CryptoServiceError(
            f'Quidax refused the {order.to_coin.upper()} buy: {exc.message}. Nothing was credited — '
            'try again later or use Refund NGN.',
            code='quidax_refused',
        )

    buy_order_id = str((payload.get('data') or {}).get('id') or '')
    _log(
        order,
        'quidax_buy_leg_sent',
        f'market={to_market} volume={_plain_decimal(bought)} quidax_order_id={buy_order_id} (admin)',
    )
    order = _complete_swap(order, buy_order_id, debit_source=False, bought=bought)
    _log(
        order,
        'admin_swap_compensated',
        f'Bought and credited {order.to_coin_amount} {order.to_coin.upper()}. {note}'.strip(),
    )
    order.refresh_from_db()
    return order
