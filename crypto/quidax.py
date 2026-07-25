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

import time

import requests
from django.conf import settings

QUIDAX_BASE = 'https://openapi.quidax.io/exchange-open-api/api/v1'
_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


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


def _request(method: str, path: str, **kwargs) -> dict:
    url = f'{QUIDAX_BASE}{path}'
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            response = requests.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(0.5 * (2**attempt))
            continue

        if response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
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


# ─── Deposit addresses ──────────────────────────────────────────────────────


def list_deposit_addresses(user_id: str, currency: str) -> dict:
    """GET /users/{user_id}/wallets/{currency}/addresses"""
    return _request('GET', f'/users/{user_id}/wallets/{currency}/addresses')


def create_deposit_address(user_id: str, currency: str, *, network: str | None = None) -> dict:
    """POST /users/{user_id}/wallets/{currency}/addresses"""
    payload = {'network': network} if network else {}
    return _request('POST', f'/users/{user_id}/wallets/{currency}/addresses', json=payload)


# ─── Orders (market buy/sell) ───────────────────────────────────────────────


def create_instant_order(*, market: str, side: str, volume: str, user_id: str = 'me') -> dict:
    """POST /users/{user_id}/orders — market order (buy or sell)."""
    return _request(
        'POST',
        f'/users/{user_id}/orders',
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
