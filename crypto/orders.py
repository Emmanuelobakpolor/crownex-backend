"""Order execution: buy (this phase), sell and swap (later phases).

Every path here starts from an already-locked CryptoQuote — rate, fee, and
amounts are always copied from the quote, never recomputed here. See
services.py for quote creation/locking and models.py for the CryptoOrder
status lifecycle.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction

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
    release_reserved_crypto,
    reserve_crypto,
)


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


def _plain_decimal(amount: Decimal) -> str:
    """Decimal as a plain string — Quidax expects '1' / '0.0011', not the
    scientific notation ('1E+2') Python's Decimal can produce."""
    normalized = amount.normalize()
    _sign, _digits, exponent = normalized.as_tuple()
    if exponent >= 0:
        return str(int(normalized))
    return format(normalized, 'f')


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
        return _fail_order(order, f'Swap sell leg failed: {exc.message}', refund_ngn=False)

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
        f'Could not buy {order.to_coin.upper()} ({reason}). '
        f'₦{net_ngn} was credited to your NGN wallet instead.'
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
            order, f'Insufficient {order.coin.upper()} balance for swap.', refund_ngn=False
        )

    _log(order, 'reserved_balance', f'Reserved {order.coin_amount} {order.coin.upper()}.')
    return _execute_quidax_swap(order)


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
