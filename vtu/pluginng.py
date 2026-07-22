"""Thin client for the PluginNG REST API (airtime, data purchases).

Every call is server-side only — the bearer token never reaches the app.
Docs: https://documenter.getpostman.com/view/32987010/2sAYJAeHjy

PluginNG's own transaction status codes (used in purchase, requery, and
webhook responses): 1 = success, 0 = pending, 4 = failed, 2 = manually
reversed. See models.PLUGINNG_STATUS_MAP.
"""

from __future__ import annotations

import requests
from django.conf import settings

PLUGINNG_BASE_URL = getattr(settings, 'PLUGINNG_BASE_URL', 'https://pluginng.com/api')
_TIMEOUT = 30


class PluginNGError(Exception):
    """Raised when PluginNG returns a non-2xx response or a network error."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {settings.PLUGINNG_TOKEN}',
        'Accept': 'application/json',
    }


def _request(method: str, path: str, **kwargs) -> dict:
    url = f'{PLUGINNG_BASE_URL}{path}'
    try:
        response = requests.request(
            method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise PluginNGError(f'Could not reach PluginNG: {exc}') from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise PluginNGError('PluginNG returned a non-JSON response.') from exc

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
