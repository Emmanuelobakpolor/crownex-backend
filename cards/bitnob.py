"""Thin client for the Bitnob Virtual Cards API.

Every call is server-side only — BITNOB_CLIENT_SECRET never reaches the
app. Docs: https://bitnob.dev/api-reference

Unlike every other integration in this codebase (Quidax, Flutterwave,
Reloadly, Dojah — all a static bearer-style secret), Bitnob requires each
request to be signed with HMAC-SHA256:

  string_to_sign = "CLIENT_ID:TIMESTAMP:NONCE:PAYLOAD"
  signature      = hex(HMAC_SHA256(string_to_sign, CLIENT_SECRET))

sent as four headers: X-Auth-Client, X-Auth-Timestamp, X-Auth-Nonce,
X-Auth-Signature. TIMESTAMP is Unix seconds, NONCE is 16 random bytes
hex-encoded, PAYLOAD is the exact JSON body string (empty string for a
bodyless request) — the same serialized string is both signed and sent,
so a body is only ever json.dumps'd once per request.

All amounts are micro-units (1,000,000 = 1 whole currency unit) — see
cards/services.py for the conversion helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

import requests
from django.conf import settings

_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class BitnobError(Exception):
    """Raised when Bitnob returns a non-2xx response or a network error."""

    def __init__(
        self,
        message: str,
        payload: dict | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}
        self.status_code = status_code
        self.error_code = error_code


def _payload_string(body: dict | None) -> str:
    if not body:
        return ''
    return json.dumps(body, separators=(',', ':'))


def _auth_headers(payload: str) -> dict:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    string_to_sign = f'{settings.BITNOB_CLIENT_ID}:{timestamp}:{nonce}:{payload}'
    signature = hmac.new(
        settings.BITNOB_CLIENT_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return {
        'X-Auth-Client': settings.BITNOB_CLIENT_ID,
        'X-Auth-Timestamp': timestamp,
        'X-Auth-Nonce': nonce,
        'X-Auth-Signature': signature,
    }


def _parse(response: requests.Response) -> dict:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise BitnobError(
            'Bitnob returned a non-JSON response.', status_code=response.status_code
        ) from exc


def _request(method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> dict:
    url = f'{settings.BITNOB_BASE_URL}{path}'
    payload = _payload_string(body)
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        headers = {
            **_auth_headers(payload),
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                data=payload if body is not None else None,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(0.5 * (2**attempt))
            continue

        if response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
            time.sleep(0.5 * (2**attempt))
            continue

        parsed = _parse(response)
        if not response.ok:
            error = parsed.get('error') if isinstance(parsed, dict) else None
            message = parsed.get('message') if isinstance(parsed, dict) else None
            raise BitnobError(
                message or f'Bitnob returned {response.status_code}.',
                parsed if isinstance(parsed, dict) else {},
                status_code=response.status_code,
                error_code=(error or {}).get('code') if isinstance(error, dict) else None,
            )
        return parsed

    raise BitnobError(f'Could not reach Bitnob: {last_exc}')


# ─── Cards ──────────────────────────────────────────────────────────────────


def create_card(
    *,
    amount_micro: int,
    currency: str,
    name: str,
    reference: str,
    customer: dict,
    card_brand: str | None = None,
    webhook_url: str | None = None,
) -> dict:
    """POST /api/cards — customer is created implicitly from the nested
    `customer` object; no separate customer-create call is needed."""
    body = {
        'card_type': 'virtual',
        'currency': currency,
        'amount': amount_micro,
        'name': name,
        'reference': reference,
        'customer': customer,
    }
    if card_brand:
        body['card_brand'] = card_brand
    if webhook_url:
        body['webhook_url'] = webhook_url
    return _request('POST', '/api/cards', body=body)


def get_card(card_id: str) -> dict:
    """GET /api/cards/:cardId"""
    return _request('GET', f'/api/cards/{card_id}')


def list_cards(*, card_type: str | None = None, status: str | None = None, search: str | None = None) -> dict:
    """GET /api/cards — company-wide list, used by admin visibility."""
    params = {k: v for k, v in {'card_type': card_type, 'status': status, 'search': search}.items() if v}
    return _request('GET', '/api/cards', params=params)


def get_cards_by_customer(customer_id: str) -> dict:
    """GET /api/customers/:customerId/cards"""
    return _request('GET', f'/api/customers/{customer_id}/cards')


def fund_or_withdraw(card_id: str, *, amount_micro: int, type_: str, reference: str) -> dict:
    """POST /api/cards/:cardId/balance — type_ is 'fund' or 'withdraw'.
    Async: response comes back pending, final state via webhook/poll."""
    body = {'amount': amount_micro, 'type': type_, 'reference': reference}
    return _request('POST', f'/api/cards/{card_id}/balance', body=body)


def update_status(card_id: str, status: str) -> dict:
    """POST /api/cards/:cardId/status — status is 'frozen' or 'active'."""
    return _request('POST', f'/api/cards/{card_id}/status', body={'status': status})


def terminate_card(card_id: str, reason: str) -> dict:
    """DELETE /api/cards/:cardId — rejected with CARD_TERMINATION_COOLDOWN
    if the card is under 24h old."""
    return _request('DELETE', f'/api/cards/{card_id}', body={'reason': reason})
