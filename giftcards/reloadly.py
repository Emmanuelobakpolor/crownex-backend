"""Thin client for the Reloadly Gift Cards API.

Every call is server-side only — client_secret never reaches the app.
Docs: https://developers.reloadly.com/giftcards

Auth is OAuth2 client_credentials against a fixed auth host, scoped via
`audience` to whichever gift-cards host (sandbox or live) we're calling.
The access token is cached until its own `expires_in`, refreshed ~60s
early so a request never races an about-to-expire token.
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.core.cache import cache

_AUTH_URL = 'https://auth.reloadly.com/oauth/token'
_TIMEOUT = 20
_TOKEN_CACHE_KEY = 'giftcards:reloadly:token'
_EARLY_REFRESH_SECONDS = 60


class ReloadlyError(Exception):
    """Raised when Reloadly returns a non-2xx response or a network error."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


def _base_url() -> str:
    return (
        'https://giftcards-sandbox.reloadly.com'
        if settings.RELOADLY_SANDBOX
        else 'https://giftcards.reloadly.com'
    )


def _login() -> str:
    """POST to Reloadly's auth host — exchange client credentials for a
    fresh access token scoped to the gift cards API."""
    try:
        response = requests.post(
            _AUTH_URL,
            json={
                'client_id': settings.RELOADLY_CLIENT_ID,
                'client_secret': settings.RELOADLY_CLIENT_SECRET,
                'grant_type': 'client_credentials',
                'audience': _base_url(),
            },
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ReloadlyError(f'Could not reach Reloadly: {exc}') from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ReloadlyError(
            'Reloadly returned a non-JSON response during auth.'
        ) from exc

    if not response.ok:
        raise ReloadlyError(
            payload.get('error_description')
            or payload.get('message')
            or f'Reloadly auth failed ({response.status_code}).',
            payload,
        )

    token = payload.get('access_token')
    if not token:
        raise ReloadlyError('Reloadly auth response did not include an access_token.', payload)

    ttl = int(payload.get('expires_in') or 3600) - _EARLY_REFRESH_SECONDS
    cache.set(_TOKEN_CACHE_KEY, token, max(ttl, 60))
    return token


def _get_token(*, force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = cache.get(_TOKEN_CACHE_KEY)
        if cached:
            return cached
    return _login()


def _headers(token: str) -> dict:
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/com.reloadly.giftcards-v1+json',
        'Content-Type': 'application/json',
    }


def _send(method: str, url: str, token: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, url, headers=_headers(token), timeout=_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise ReloadlyError(f'Could not reach Reloadly: {exc}') from exc


def _parse(response: requests.Response):
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise ReloadlyError('Reloadly returned a non-JSON response.') from exc


def _request(method: str, path: str, **kwargs):
    """Single entry point for every Reloadly call: attaches the cached
    access token, and — if it turns out to be stale — re-authenticates
    exactly once and retries before giving up."""
    url = f'{_base_url()}{path}'

    token = _get_token()
    response = _send(method, url, token, **kwargs)

    if response.status_code == 401:
        token = _get_token(force_refresh=True)
        response = _send(method, url, token, **kwargs)

    payload = _parse(response)

    if not response.ok:
        message = payload.get('errorMessage') or payload.get('message') if isinstance(payload, dict) else None
        raise ReloadlyError(
            message or f'Reloadly returned {response.status_code}.',
            payload if isinstance(payload, dict) else {},
        )

    return payload


def get_products(*, country_code: str, product_name: str | None = None, size: int = 200, page: int = 1):
    """GET /products?countryCode=..&productName=..&size=..&page=.."""
    params = {'countryCode': country_code, 'size': size, 'page': page}
    if product_name:
        params['productName'] = product_name
    return _request('GET', '/products', params=params)


def create_order(
    *, product_id: int, quantity: int, unit_price: str, custom_identifier: str,
    sender_name: str, recipient_email: str,
) -> dict:
    """POST /orders — places (and, for most brands, immediately fulfils) a
    gift card order."""
    return _request(
        'POST',
        '/orders',
        json={
            'productId': product_id,
            'quantity': quantity,
            'unitPrice': unit_price,
            'customIdentifier': custom_identifier,
            'senderName': sender_name,
            'recipientEmail': recipient_email,
            'preOrder': False,
        },
    )


def get_order(transaction_id) -> dict:
    """GET /orders/transactions/{transactionId} — the authoritative order
    record, used to fetch the redeemed card for brands that fulfil after
    the initial POST /orders response (see the webhook handler)."""
    return _request('GET', f'/orders/transactions/{transaction_id}')
