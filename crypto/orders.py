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


def _buy_market_volume_ngn(order: CryptoOrder) -> str:
    """Whole-naira floor of the crypto's own NGN value — NOT including our
    fee. Quidax only ever buys the notional; the fee stays with us as
    platform margin in the NGN already collected from the user."""
    notional = (order.rate_ngn * order.coin_amount).to_integral_value(rounding=ROUND_DOWN)
    return str(int(notional))


def _execute_quidax_buy(order: CryptoOrder, *, refund_on_fail: bool) -> CryptoOrder:
    """Places the market buy on Quidax and finalizes the order — credits the
    user's internal CryptoWallet on success. Assumes payment is already
    secured (status is payment_received going in)."""
    market = f'{order.coin}ngn'
    volume = _buy_market_volume_ngn(order)

    try:
        payload = quidax.create_instant_order(
            market=market, side='buy', volume=volume, user_id=settings.QUIDAX_USER_ID
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Buy order request failed: {exc.message}')
        return _fail_order(order, f'Quidax buy failed: {exc.message}', refund_ngn=refund_on_fail)

    data = payload.get('data') or {}
    quidax_order_id = str(data.get('id') or '')
    _log(
        order,
        'quidax_buy_sent',
        f'market={market} volume={volume} quidax_order_id={quidax_order_id}',
    )

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
    """Crypto quantity as a plain decimal string — Quidax expects '1', not
    the scientific notation ('1E+2') Python's Decimal can produce."""
    normalized = order.coin_amount.normalize()
    _sign, _digits, exponent = normalized.as_tuple()
    if exponent >= 0:
        return str(int(normalized))
    return format(normalized, 'f')


def _execute_quidax_sell(order: CryptoOrder) -> CryptoOrder:
    """Places the market sell on Quidax and finalizes — on success, debits
    the reserved crypto and credits the NGN payout; on failure, releases
    the reservation back to available (nothing external moved yet, so
    there's nothing to refund on the NGN side)."""
    market = f'{order.coin}ngn'
    volume = _sell_volume_crypto(order)

    try:
        payload = quidax.create_instant_order(
            market=market, side='sell', volume=volume, user_id=settings.QUIDAX_USER_ID
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Sell order request failed: {exc.message}')
        release_reserved_crypto(order.user, order.coin, order.coin_amount)
        _log(order, 'reservation_released', f'Released {order.coin_amount} {order.coin.upper()} back to available.')
        return _fail_order(order, f'Quidax sell failed: {exc.message}', refund_ngn=False)

    data = payload.get('data') or {}
    quidax_order_id = str(data.get('id') or '')
    _log(
        order,
        'quidax_sell_sent',
        f'market={market} volume={volume} quidax_order_id={quidax_order_id}',
    )

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


def _swap_leg2_volume_ngn(order: CryptoOrder) -> str:
    """Whole-naira floor of (notional - fee) — the NGN actually available to
    spend on the destination coin after our margin is taken out."""
    notional = order.rate_ngn * order.coin_amount
    net_ngn = (notional - order.fee_ngn).to_integral_value(rounding=ROUND_DOWN)
    return str(int(net_ngn))


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
    to_market = f'{order.to_coin}ngn'
    sell_volume = _sell_volume_crypto(order)

    try:
        sell_payload = quidax.create_instant_order(
            market=from_market, side='sell', volume=sell_volume, user_id=settings.QUIDAX_USER_ID
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Swap sell leg ({order.coin.upper()}) failed: {exc.message}')
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

    buy_volume = _swap_leg2_volume_ngn(order)
    try:
        buy_payload = quidax.create_instant_order(
            market=to_market, side='buy', volume=buy_volume, user_id=settings.QUIDAX_USER_ID
        )
    except QuidaxError as exc:
        _log(order, 'quidax_error', f'Swap buy leg ({order.to_coin.upper()}) failed: {exc.message}')
        debit_reserved_crypto(order.user, order.coin, order.coin_amount)
        _log(
            order,
            'reservation_debited_needs_review',
            f'{order.coin_amount} {order.coin.upper()} already sold on the sell leg — not '
            f'released. Admin must review and manually credit {order.to_coin.upper()} or '
            'compensate the user.',
        )
        return _fail_order(
            order, f'Swap buy leg failed after sell leg succeeded: {exc.message}', refund_ngn=False
        )

    buy_data = buy_payload.get('data') or {}
    buy_order_id = str(buy_data.get('id') or '')
    _log(
        order,
        'quidax_buy_leg_sent',
        f'market={to_market} volume={buy_volume} quidax_order_id={buy_order_id}',
    )

    with transaction.atomic():
        locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status == OrderStatus.COMPLETED:
            return locked
        locked.quidax_order_id = buy_order_id or locked.quidax_order_id
        locked.status = OrderStatus.COMPLETED
        locked.save(update_fields=['quidax_order_id', 'status', 'updated_at'])
        debit_reserved_crypto(locked.user, locked.coin, locked.coin_amount)
        credit_crypto_available(locked.user, locked.to_coin, locked.to_coin_amount)

    _log(order, 'order_completed', f'Credited {order.to_coin_amount} {order.to_coin.upper()} to wallet.')
    order.refresh_from_db()
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
