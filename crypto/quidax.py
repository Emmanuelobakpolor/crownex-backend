"""Thin client for the Quidax Business API (crypto trading, wallets, withdrawals).

Every call is server-side only — QUIDAX_SECRET_KEY never reaches the app.
Docs: https://docs.quidax.com

Auth is a static bearer token, unlike PluginNG's login-and-cache dance —
Quidax business API keys don't expire on their own. Calls retry on
429/5xx with exponential backoff (max 3 attempts) since the market/order
endpoints occasionally blip under load; a network-level failure retries
the same way.
"""

from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

QUIDAX_BASE = 'https://openapi.quidax.io/exchange-open-api/api/v1'
_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}

logger = logging.getLogger(__name__)


class QuidaxError(Exception):
    """Raised when Quidax returns a non-2xx response or a network error."""

    def __init__(self, message: str, payload: dict | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}
        self.status_code = status_code


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {settings.QUIDAX_SECRET_KEY}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }


def _parse(response: requests.Response) -> dict:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise QuidaxError(
            'Quidax returned a non-JSON response.', status_code=response.status_code
        ) from exc


def _request(method: str, path: str, *, retry: bool = True, **kwargs) -> dict:
    url = f'{QUIDAX_BASE}{path}'
    last_exc: Exception | None = None
    max_attempts = _MAX_RETRIES if retry else 1

    for attempt in range(max_attempts):
        try:
            response = requests.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (2**attempt))
            continue

        if response.status_code in _RETRY_STATUSES and attempt < max_attempts - 1:
            time.sleep(0.5 * (2**attempt))
            continue

        payload = _parse(response)
        if not response.ok:
            message = payload.get('message') if isinstance(payload, dict) else None
            raise QuidaxError(
                message or f'Quidax returned {response.status_code}.',
                payload if isinstance(payload, dict) else {},
                status_code=response.status_code,
            )
        return payload

    raise QuidaxError(f'Could not reach Quidax: {last_exc}')


# ─── Market data ────────────────────────────────────────────────────────────


def get_all_tickers() -> dict:
    """GET /markets/tickers — every market's last price in one call."""
    return _request('GET', '/markets/tickers')


# ─── Sub-accounts ───────────────────────────────────────────────────────────


def create_sub_account(*, email: str, first_name: str, last_name: str) -> dict:
    """POST /users — provisions a Quidax sub-account for a CrownEx user."""
    return _request(
        'POST',
        '/users',
        json={'email': email, 'first_name': first_name, 'last_name': last_name},
    )


def find_sub_account_by_email(email: str) -> dict | None:
    """GET /users — Quidax has no search-by-email endpoint, only "fetch all
    sub-accounts", so we page through it and filter client-side. Used to
    reconcile the "sub account with this email already exists" case: a
    sub-account exists on Quidax's side but our local QuidaxSubAccount row
    is missing (e.g. a prior signup succeeded remotely but never saved
    locally). Returns the matching user dict, or None if not found."""
    target = email.strip().lower()
    seen_ids: set[str] = set()
    scanned = 0
    for page in range(1, 201):
        payload = _request('GET', '/users', params={'page': page})
        rows = _extract_rows(payload)
        if page == 1:
            # Shape diagnostics only (no emails/PII) so a miss is debuggable.
            data = payload.get('data') if isinstance(payload, dict) else None
            logger.warning(
                'Quidax /users lookup: payload keys=%s data type=%s first row keys=%s',
                sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
                type(data).__name__,
                sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else None,
            )
        if not rows:
            break
        new_ids = {str(r.get('id')) for r in rows if isinstance(r, dict)} - seen_ids
        if not new_ids:
            # Quidax ignored ?page= and returned the same rows again.
            break
        seen_ids |= new_ids
        for row in rows:
            if isinstance(row, dict) and _row_email(row) == target:
                return row
        scanned += len(rows)
    logger.warning('Quidax /users lookup found no match after %s pages, %s rows.', page, scanned)
    return None


def _extract_rows(payload) -> list:
    """Quidax wraps lists inconsistently: data may be the list itself or a
    dict holding it (data.users / data.data / data.items)."""
    data = payload.get('data') if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('users', 'data', 'items', 'results'):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _row_email(row: dict) -> str:
    email = row.get('email') or (row.get('user') or {}).get('email') or ''
    return str(email).strip().lower()


# ─── Deposit addresses ──────────────────────────────────────────────────────


def list_deposit_addresses(user_id: str, currency: str) -> dict:
    """GET /users/{user_id}/wallets/{currency}/addresses"""
    return _request('GET', f'/users/{user_id}/wallets/{currency}/addresses')


def create_deposit_address(user_id: str, currency: str, *, network: str | None = None) -> dict:
    """POST /users/{user_id}/wallets/{currency}/addresses"""
    payload = {'network': network} if network else {}
    return _request('POST', f'/users/{user_id}/wallets/{currency}/addresses', json=payload)


# ─── Deposits ───────────────────────────────────────────────────────────────


def list_deposits(user_id: str, currency: str) -> list:
    """GET /users/{user_id}/deposits?currency= — used to reconcile deposits
    whose deposit.successful webhook never arrived."""
    payload = _request('GET', f'/users/{user_id}/deposits', params={'currency': currency})
    return _extract_rows(payload)


# ─── Orders (market buy/sell) ───────────────────────────────────────────────


def create_instant_order(
    *, market: str, side: str, volume: str, user_id: str = 'me', retry: bool = True
) -> dict:
    """POST /users/{user_id}/orders — market order (buy or sell).

    Orders carry no client reference, so retrying after a 5xx/timeout that
    Quidax actually executed places a second order. retry=False surfaces
    any such failure as-is (status_code None or 5xx = outcome unknown)."""
    return _request(
        'POST',
        f'/users/{user_id}/orders',
        retry=retry,
        json={'market': market, 'side': side, 'ord_type': 'market', 'volume': volume},
    )


def get_order(order_id, *, user_id: str = 'me') -> dict:
    """GET /users/{user_id}/orders/{order_id}"""
    return _request('GET', f'/users/{user_id}/orders/{order_id}')


# ─── Withdrawals ────────────────────────────────────────────────────────────


def create_withdrawal(
    *,
    currency: str,
    amount: str,
    address: str,
    network: str | None = None,
    reference: str | None = None,
    user_id: str = 'me',
) -> dict:
    """POST /users/{user_id}/withdraws"""
    payload = {'currency': currency, 'amount': amount, 'fund_uid': address}
    if network:
        payload['network'] = network
    if reference:
        payload['reference'] = reference
    return _request('POST', f'/users/{user_id}/withdraws', json=payload)


# ─── Sub-account -> master sweeps ───────────────────────────────────────────
#
# Per Quidax's "Creating an Internal Withdrawal from a Sub-Account to the
# Main Account" guide: the same withdraws endpoint, called on the
# sub-account with fund_uid = the master account's id. Settles as type
# internal_transfer (no txid) and finishes with status done | rejected,
# also announced via withdraw.successful / withdraw.rejected.


def get_master_account() -> dict:
    """GET /users/me — data.id is the fund_uid sweeps are sent to."""
    return _request('GET', '/users/me')


def get_wallet(user_id: str, currency: str) -> dict:
    """GET /users/{user_id}/wallets/{currency}"""
    return _request('GET', f'/users/{user_id}/wallets/{currency}')


def list_wallets(user_id: str) -> list:
    """GET /users/{user_id}/wallets — every currency's balance in one call."""
    return _extract_rows(_request('GET', f'/users/{user_id}/wallets'))


def create_internal_transfer(
    *, from_user_id: str, to_user_id: str, currency: str, amount: str, reference: str
) -> dict:
    """POST /users/{from_user_id}/withdraws with fund_uid=<to_user_id>.
    reference is mandatory here: Quidax rejects a reused one, which is what
    makes a retried request (ours or _request's) unable to move funds twice."""
    return _request(
        'POST',
        f'/users/{from_user_id}/withdraws',
        json={
            'currency': currency,
            'amount': amount,
            'fund_uid': to_user_id,
            'reference': reference,
            'transaction_note': 'CrownEx deposit sweep',
            'narration': 'CrownEx deposit sweep',
        },
    )


def get_withdrawal_by_reference(user_id: str, reference: str) -> dict:
    """GET /users/{user_id}/withdraws/reference/{reference}"""
    return _request('GET', f'/users/{user_id}/withdraws/reference/{reference}')


# ─── Instant swap (quoted price) ────────────────────────────────────────────
#
# Used by swaps when QUIDAX_INSTANT_SWAP_ENABLED (orders._execute_instant_swap).
# Quote (exactly one of from_amount / to_amount), then confirm within 15s;
# the confirm response, GET swap_transactions/{id} and the
# swap_transaction.completed|failed webhooks carry status, received_amount
# and execution_price. Checked against docs.quidax.io/reference.


def temporary_swap_quotation(
    *, from_currency: str, to_currency: str, from_amount: str, user_id: str = 'me'
) -> dict:
    """POST /users/{user_id}/temporary_swap_quotation — price preview only,
    creates nothing that can be confirmed."""
    return _request(
        'POST',
        f'/users/{user_id}/temporary_swap_quotation',
        json={'from_currency': from_currency, 'to_currency': to_currency, 'from_amount': from_amount},
    )


def create_swap_quotation(
    *, from_currency: str, to_currency: str, from_amount: str, user_id: str = 'me'
) -> dict:
    """POST /users/{user_id}/swap_quotation"""
    return _request(
        'POST',
        f'/users/{user_id}/swap_quotation',
        json={'from_currency': from_currency, 'to_currency': to_currency, 'from_amount': from_amount},
    )


def confirm_swap_quotation(quotation_id: str, *, user_id: str = 'me') -> dict:
    """POST /users/{user_id}/swap_quotation/{id}/confirm — executes the swap.
    Never retried automatically (a quotation can only be used once anyway)."""
    return _request('POST', f'/users/{user_id}/swap_quotation/{quotation_id}/confirm', retry=False)


def get_swap_transaction(swap_id: str, *, user_id: str = 'me') -> dict:
    """GET /users/{user_id}/swap_transactions/{id} — status initiated |
    completed | failed, with received_amount / execution_price once done."""
    return _request('GET', f'/users/{user_id}/swap_transactions/{swap_id}')


def list_swap_transactions(*, user_id: str = 'me') -> list:
    """GET /users/{user_id}/swap_transactions — each row carries its
    swap_quotation.id, which is how a swap whose confirm response was lost
    is found again. Quidax documents no filters or pagination."""
    return _extract_rows(_request('GET', f'/users/{user_id}/swap_transactions'))
