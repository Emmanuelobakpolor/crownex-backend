"""Thin client for the PluginNG REST API (airtime, data purchases).

Every call is server-side only — no PluginNG credential ever reaches the
app. Docs: https://documenter.getpostman.com/view/32987010/2sAYJAeHjy

PluginNG's bearer token isn't a static API key — it's issued by POST
/login (email + password) and goes stale on its own schedule (observed:
a previously-working token started returning 401 with no time-based
explanation, most likely invalidated by a later login to the same
account elsewhere — e.g. Postman). A token pasted into an env var by
hand has no way to recover from that except a manual redeploy, so
instead we log in ourselves, cache the token, and transparently
re-authenticate on the first 401.

PluginNG's own transaction status codes (used in purchase, requery, and
webhook responses): 1 = success, 0 = pending, 4 = failed, 2 = manually
reversed. See models.PLUGINNG_STATUS_MAP.
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.core.cache import cache

PLUGINNG_BASE_URL = getattr(settings, 'PLUGINNG_BASE_URL', 'https://pluginng.com/api')
_TIMEOUT = 30
_TOKEN_CACHE_KEY = 'vtu:pluginng:token'
_TOKEN_CACHE_TTL = 50 * 60  # 50 min — PluginNG doesn't document token expiry,
# so we refresh proactively well inside any plausible 1-hour-style window.


class PluginNGError(Exception):
    """Raised when PluginNG returns a non-2xx response or a network error."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


def _login() -> str:
    """POST /login — exchange account credentials for a fresh bearer token."""
    try:
        response = requests.post(
            f'{PLUGINNG_BASE_URL}/login',
            json={
                'email': settings.PLUGINNG_EMAIL,
                'password': settings.PLUGINNG_PASSWORD,
            },
            headers={'Accept': 'application/json'},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise PluginNGError(f'Could not reach PluginNG: {exc}') from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise PluginNGError(
            'PluginNG returned a non-JSON response during login.'
        ) from exc

    if not response.ok:
        raise PluginNGError(
            payload.get('message') or f'PluginNG login failed ({response.status_code}).',
            payload,
        )

    token = (payload.get('data') or {}).get('token')
    if not token:
        raise PluginNGError('PluginNG login response did not include a token.', payload)

    cache.set(_TOKEN_CACHE_KEY, token, _TOKEN_CACHE_TTL)
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
        'Accept': 'application/json',
    }


def _send(method: str, url: str, token: str, **kwargs) -> requests.Response:
    try:
        return requests.request(
            method, url, headers=_headers(token), timeout=_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise PluginNGError(f'Could not reach PluginNG: {exc}') from exc


def _parse(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError as exc:
        raise PluginNGError('PluginNG returned a non-JSON response.') from exc


def _looks_unauthenticated(response: requests.Response, payload: dict) -> bool:
    """PluginNG sometimes signals a dead token as a plain 401, but Laravel-style
    APIs like this one can also answer 200 with an "Unauthenticated" message
    in the body — catch both so a stale cached token always triggers a retry."""
    if response.status_code == 401:
        return True
    message = payload.get('message')
    return isinstance(message, str) and 'unauthenticat' in message.lower()


def _request(method: str, path: str, **kwargs) -> dict:
    """Single entry point for every PluginNG call: attaches the cached
    bearer token, and — if the token turns out to be stale — re-authenticates
    exactly once and retries before giving up."""
    url = f'{PLUGINNG_BASE_URL}{path}'

    token = _get_token()
    response = _send(method, url, token, **kwargs)
    payload = _parse(response)

    if _looks_unauthenticated(response, payload):
        token = _get_token(force_refresh=True)
        response = _send(method, url, token, **kwargs)
        payload = _parse(response)

    if not response.ok:
        raise PluginNGError(
            payload.get('message')
            or f'PluginNG returned {response.status_code}.',
            payload,
        )

    return payload


def get_plans() -> dict:
    """GET /get/plans — full catalogue: airtime networks, data plans, and more."""
    return _request('GET', '/get/plans')


def buy_airtime(
    *, amount: str, phonenumber: str, subcategory_id: str, custom_reference: str
) -> dict:
    """POST /purchase/airtime"""
    return _request(
        'POST',
        '/purchase/airtime',
        data={
            'amount': amount,
            'phonenumber': phonenumber,
            'subcategory_id': subcategory_id,
            'custom_reference': custom_reference,
        },
    )


def buy_data(
    *, plan_id: str, phonenumber: str, subcategory_id: str, custom_reference: str
) -> dict:
    """POST /purchase/data"""
    return _request(
        'POST',
        '/purchase/data',
        data={
            'plan_id': plan_id,
            'phonenumber': phonenumber,
            'subcategory_id': subcategory_id,
            'custom_reference': custom_reference,
        },
    )


def requery(custom_reference: str) -> dict:
    """GET /requery/{custom_reference} — check a purchase's current status."""
    return _request('GET', f'/requery/{custom_reference}')


def fetch_bouquet(plan: str) -> dict:
    """GET /fetch/bouquet?plan=<title> — cable bouquets or electricity variations
    for a given biller title (e.g. "gotv", "Eko-Electric")."""
    return _request('GET', '/fetch/bouquet', params={'plan': plan})


def verify_card(*, plan: str, cardno: str, meter_type: str | None = None) -> dict:
    """POST /verify/card — resolves a smartcard (cable) or meter (electricity,
    pass meter_type) number to the customer's name before charging."""
    data = {'plan': plan, 'cardno': cardno}
    if meter_type:
        data['type'] = meter_type
    return _request('POST', '/verify/card', data=data)


def buy_cable(
    *,
    plan: str,
    phonenumber: str,
    amount: str,
    cardno: str,
    variation_code: str,
    custom_reference: str,
) -> dict:
    """POST /purchase/cable"""
    return _request(
        'POST',
        '/purchase/cable',
        data={
            'plan': plan,
            'phonenumber': phonenumber,
            'amount': amount,
            'cardno': cardno,
            'variation_code': variation_code,
            'custom_reference': custom_reference,
        },
    )


def buy_electricity(
    *,
    plan: str,
    phonenumber: str,
    amount: str,
    cardno: str,
    variation_code: str,
    service_id: str,
    custom_reference: str,
) -> dict:
    """POST /purchase/electricity"""
    return _request(
        'POST',
        '/purchase/electricity',
        data={
            'plan': plan,
            'phonenumber': phonenumber,
            'amount': amount,
            'cardno': cardno,
            'variation_code': variation_code,
            'serviceID': service_id,
            'custom_reference': custom_reference,
        },
    )
