"""Sub-account provisioning, deposit address lookup, and the Quidax deposit
webhook.

get_or_create_sub_account is called from two places: eagerly, by the
post_save signal in signals.py (fires right after a user is created), and
lazily here as a fallback the first time someone actually requests a
deposit address — in case the signal's attempt failed (e.g. Quidax was
down at signup) or ran before QUIDAX_SECRET_KEY was configured. Either way
it never blocks signup itself. A management command
(backfill_quidax_subaccounts) also covers provisioning ahead of time for
existing users.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from django.db import transaction

from . import quidax
from .models import CryptoDepositAddress, CryptoDepositEvent, CryptoOrder, OrderStatus, QuidaxSubAccount
from .orders import _log as _log_order_event
from .quidax import QuidaxError
from .services import CryptoServiceError, _validate_coin, credit_crypto_available

logger = logging.getLogger(__name__)

_ADDRESS_POLL_ATTEMPTS = 5
_ADDRESS_POLL_DELAY_SECONDS = 1


def get_or_create_sub_account(user) -> QuidaxSubAccount:
    existing = QuidaxSubAccount.objects.filter(user=user).first()
    if existing:
        return existing

    first_name, _, last_name = (user.full_name or user.email).partition(' ')
    try:
        payload = quidax.create_sub_account(
            email=user.email, first_name=first_name or user.email, last_name=last_name or '-'
        )
    except QuidaxError as exc:
        logger.error(
            'Quidax sub-account creation failed for %s: %s (payload=%s)',
            user.email,
            exc.message,
            exc.payload,
        )
        raise CryptoServiceError(
            f'Could not set up your crypto account: {exc.message}',
            code='quidax_unreachable',
            status=502,
        )

    data = payload.get('data') or {}
    quidax_id = str(data.get('id') or '')
    if not quidax_id:
        logger.error('Quidax sub-account response had no id for %s: %s', user.email, payload)
        raise CryptoServiceError(
            'Quidax did not return a sub-account id.', code='quidax_error', status=502
        )

    sub_account, _ = QuidaxSubAccount.objects.get_or_create(
        user=user, defaults={'quidax_user_id': quidax_id}
    )
    return sub_account


def _pick_address(payload: dict, network_key: str) -> tuple[str, str] | None:
    data = payload.get('data')
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for row in rows:
        if not row:
            continue
        row_network = str(row.get('network') or '').upper()
        if network_key and row_network and row_network != network_key:
            continue
        address = row.get('address')
        if address:
            return address, str(row.get('id') or '')
    return None


def get_deposit_address(user, coin: str, *, network: str | None = None) -> CryptoDepositAddress:
    coin = _validate_coin(coin)
    network_key = (network or '').upper()

    cached = CryptoDepositAddress.objects.filter(user=user, coin=coin, network=network_key).first()
    if cached:
        return cached

    sub_account = get_or_create_sub_account(user)

    try:
        list_payload = quidax.list_deposit_addresses(sub_account.quidax_user_id, coin)
    except QuidaxError as exc:
        raise CryptoServiceError(
            f'Could not load deposit address: {exc.message}', code='quidax_unreachable', status=502
        )

    found = _pick_address(list_payload, network_key)

    if not found:
        try:
            quidax.create_deposit_address(sub_account.quidax_user_id, coin, network=network or None)
        except QuidaxError as exc:
            raise CryptoServiceError(
                f'Could not create deposit address: {exc.message}',
                code='quidax_unreachable',
                status=502,
            )

        # Quidax generates some addresses asynchronously (wallet.address.generated
        # webhook) — poll briefly instead of making the client retry from scratch.
        for _ in range(_ADDRESS_POLL_ATTEMPTS):
            time.sleep(_ADDRESS_POLL_DELAY_SECONDS)
            try:
                list_payload = quidax.list_deposit_addresses(sub_account.quidax_user_id, coin)
            except QuidaxError:
                break
            found = _pick_address(list_payload, network_key)
            if found:
                break

    if not found:
        raise CryptoServiceError(
            'Deposit address is still being generated — please try again shortly.',
            code='address_pending',
            status=503,
        )

    address_value, quidax_ref = found
    record, _ = CryptoDepositAddress.objects.get_or_create(
        user=user,
        coin=coin,
        network=network_key,
        defaults={'address': address_value, 'quidax_ref': quidax_ref},
    )
    return record


# ─── Webhooks ───────────────────────────────────────────────────────────────


def handle_deposit_webhook(payload: dict) -> None:
    """deposit.successful — credit the depositor's internal CryptoWallet.
    Maps back to our user via the sub-account's quidax_user_id, since
    that's the identity Quidax's webhook payload actually carries (never
    the CrownEx user id)."""
    data = payload.get('data') or payload
    quidax_deposit_id = str(data.get('id') or '')
    quidax_user_id = str((data.get('user') or {}).get('id') or data.get('user_id') or '')
    currency = str(data.get('currency') or '').lower()
    amount = data.get('amount')

    if not quidax_deposit_id or not quidax_user_id or not currency or amount is None:
        return

    sub_account = QuidaxSubAccount.objects.filter(quidax_user_id=quidax_user_id).first()
    if not sub_account:
        return

    with transaction.atomic():
        _event, created = CryptoDepositEvent.objects.get_or_create(
            quidax_deposit_id=quidax_deposit_id,
            defaults={
                'user': sub_account.user,
                'coin': currency,
                'amount': Decimal(str(amount)),
            },
        )
        if created:
            credit_crypto_available(sub_account.user, currency, Decimal(str(amount)))


def handle_order_webhook(payload: dict) -> None:
    """order.done / order.completed — secondary confirmation only.

    Buy/sell/swap already finalize synchronously within the initial
    create_instant_order response (market orders fill immediately in the
    normal case), so this just logs receipt for orders we recognize and is
    a no-op for anything already completed — it never re-credits.
    """
    data = payload.get('data') or payload
    quidax_order_id = str(data.get('id') or '')
    if not quidax_order_id:
        return

    order = CryptoOrder.objects.filter(quidax_order_id=quidax_order_id).first()
    if order is None or order.status == OrderStatus.COMPLETED:
        return

    _log_order_event(order, 'quidax_webhook_received', f'order.done webhook for {quidax_order_id}.')
