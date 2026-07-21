"""Thin client for the Flutterwave v4 (F4B) API.

Auth is OAuth2 client-credentials: exchange FLW_CLIENT_ID/FLW_CLIENT_SECRET
for a short-lived (10 min) access token at FLW_TOKEN_URL, cache it, and
attach it as a Bearer token on every call. All of this is server-side only —
the client secret never reaches the app.

NOTE: Flutterwave's own docs are inconsistent about the v4 base URL between
different pages (api.flutterwave.cloud/f4b/{env} vs
developersandbox-api.flutterwave.com). FLW_BASE_URL defaults to the former
(sourced from their published OpenAPI spec) but is fully overridable via env
— if calls fail with connection errors once real credentials are wired up,
check this first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

import requests
from django.conf import settings
from django.core.cache import cache

_TIMEOUT = 20
_TOKEN_CACHE_KEY = 'flw_v4_access_token'


class FlutterwaveError(Exception):
    """Raised when Flutterwave auth fails or a network/server error occurs."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


def _base_url() -> str:
    return getattr(settings, 'FLW_BASE_URL', 'https://api.flutterwave.cloud/f4b/sandbox')


def _fetch_access_token() -> str:
    try:
        response = requests.post(
            settings.FLW_TOKEN_URL,
            data={
                'client_id': settings.FLW_CLIENT_ID,
                'client_secret': settings.FLW_CLIENT_SECRET,
                'grant_type': 'client_credentials',
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise FlutterwaveError(f'Could not reach Flutterwave auth: {exc}') from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise FlutterwaveError('Flutterwave auth returned a non-JSON response.') from exc

    token = payload.get('access_token')
    if response.status_code >= 400 or not token:
        raise FlutterwaveError(
            payload.get('error_description', 'Could not authenticate with Flutterwave.'),
            payload,
        )

    expires_in = int(payload.get('expires_in', 600))
    # Refresh a minute early so an in-flight request never carries a token
    # that expires mid-call.
    cache.set(_TOKEN_CACHE_KEY, token, timeout=max(expires_in - 60, 30))
    return token


def _access_token() -> str:
    return cache.get(_TOKEN_CACHE_KEY) or _fetch_access_token()


def _request(
    method: str,
    path: str,
    *,
    idempotency_key: str | None = None,
    _retry_auth: bool = True,
    **kwargs,
) -> dict:
    trace_id = f'crownex-{uuid.uuid4().hex}'
    headers = {
        'Authorization': f'Bearer {_access_token()}',
        'Content-Type': 'application/json',
        'X-Trace-Id': trace_id,
    }
    if idempotency_key:
        headers['X-Idempotency-Key'] = idempotency_key

    url = f'{_base_url()}{path}'
    try:
        response = requests.request(method, url, headers=headers, timeout=_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise FlutterwaveError(f'Could not reach Flutterwave: {exc}') from exc

    if response.status_code == 401 and _retry_auth:
        # Token may have been invalidated server-side before our cached
        # expiry — drop it and retry exactly once with a fresh one.
        cache.delete(_TOKEN_CACHE_KEY)
        return _request(
            method, path, idempotency_key=idempotency_key, _retry_auth=False, **kwargs
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise FlutterwaveError('Flutterwave returned a non-JSON response.') from exc

    if response.status_code >= 500:
        raise FlutterwaveError(
            payload.get('message', 'Flutterwave is temporarily unavailable.'), payload
        )

    return payload


# ─── Deposits (direct orders: bank transfer / USSD) ────────────────────────


def create_direct_order(
    *,
    amount: str,
    currency: str,
    reference: str,
    customer: dict,
    payment_method: dict,
) -> dict:
    """POST {FLW_ORDERS_ENDPOINT} — creates a deposit order."""
    return _request(
        'POST',
        settings.FLW_ORDERS_ENDPOINT,
        idempotency_key=reference,
        json={
            'amount': amount,
            'currency': currency,
            'reference': reference,
            'customer': customer,
            'payment_method': payment_method,
        },
    )


def get_order(order_id: str) -> dict:
    """GET {FLW_ORDER_GET_ENDPOINT}/{id} — poll/verify a deposit's status."""
    return _request('GET', f'{settings.FLW_ORDER_GET_ENDPOINT}/{order_id}')


# ─── Withdrawals (transfer recipients + transfers) ─────────────────────────


def create_recipient(*, account_number: str, bank_code: str) -> dict:
    """POST {FLW_TRANSFER_RECIPIENTS_ENDPOINT} — register a payout destination (NGN bank)."""
    return _request(
        'POST',
        settings.FLW_TRANSFER_RECIPIENTS_ENDPOINT,
        json={
            'type': 'bank_ngn',
            'bank': {'account_number': account_number, 'code': bank_code},
        },
    )


def create_transfer(
    *,
    recipient_id: str,
    amount: float,
    reference: str,
    narration: str,
    currency: str = 'NGN',
) -> dict:
    """POST {FLW_TRANSFERS_ENDPOINT} — pay out to a previously created recipient."""
    return _request(
        'POST',
        settings.FLW_TRANSFERS_ENDPOINT,
        idempotency_key=reference,
        json={
            'action': 'instant',
            'payment_instruction': {
                'recipient_id': recipient_id,
                'source_currency': currency,
                'amount': {'value': amount, 'applies_to': 'source_currency'},
            },
            'reference': reference,
            'narration': narration,
        },
    )


def get_transfer(transfer_id: str) -> dict:
    """GET {FLW_TRANSFERS_ENDPOINT}/{id} — poll/verify a withdrawal's status."""
    return _request('GET', f'{settings.FLW_TRANSFERS_ENDPOINT}/{transfer_id}')


# ─── Banks / account resolution ────────────────────────────────────────────


def get_banks(country: str = 'NG') -> dict:
    """GET {FLW_BANKS_ENDPOINT}?country=NG"""
    return _request('GET', settings.FLW_BANKS_ENDPOINT, params={'country': country})


def resolve_account(*, account_number: str, bank_code: str, currency: str = 'NGN') -> dict:
    """POST {FLW_BANK_RESOLVE_ENDPOINT}"""
    return _request(
        'POST',
        settings.FLW_BANK_RESOLVE_ENDPOINT,
        json={
            'currency': currency,
            'account': {'code': bank_code, 'number': account_number},
        },
    )


# ─── Webhook signature ──────────────────────────────────────────────────────


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256(raw body, FLW_WEBHOOK_HASH), base64-encoded, compared to
    the `flutterwave-signature` header."""
    if not signature:
        return False
    secret = settings.FLW_WEBHOOK_HASH.encode()
    computed = base64.b64encode(hmac.new(secret, raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(computed, signature)
