"""Thin client for the Flutterwave v3 REST API.

Every call is server-side only — the secret key never reaches the app.
"""

from __future__ import annotations

import requests
from django.conf import settings

FLW_BASE_URL = getattr(settings, 'FLW_BASE_URL', 'https://api.flutterwave.com/v3')
_TIMEOUT = 20


class FlutterwaveError(Exception):
    """Raised when Flutterwave returns a non-2xx response or a network error."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {settings.FLW_SECRET_KEY}',
        'Content-Type': 'application/json',
    }


def _request(method: str, path: str, **kwargs) -> dict:
    url = f'{FLW_BASE_URL}{path}'
    try:
        response = requests.request(
            method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise FlutterwaveError(f'Could not reach Flutterwave: {exc}') from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise FlutterwaveError('Flutterwave returned a non-JSON response.') from exc

    if response.status_code >= 500:
        raise FlutterwaveError(
            payload.get('message', 'Flutterwave is temporarily unavailable.'),
            payload,
        )

    return payload


def verify_transaction(tx_ref: str) -> dict:
    """GET /transactions/verify_by_reference?tx_ref=... — confirm a deposit."""
    return _request(
        'GET',
        '/transactions/verify_by_reference',
        params={'tx_ref': tx_ref},
    )


def initiate_transfer(
    *,
    account_bank: str,
    account_number: str,
    amount: float,
    narration: str,
    reference: str,
    beneficiary_name: str,
    currency: str = 'NGN',
) -> dict:
    """POST /transfers — send money out to a bank account."""
    return _request(
        'POST',
        '/transfers',
        json={
            'account_bank': account_bank,
            'account_number': account_number,
            'amount': amount,
            'narration': narration,
            'currency': currency,
            'reference': reference,
            'beneficiary_name': beneficiary_name,
        },
    )


def get_banks(country: str = 'NG') -> dict:
    """GET /banks/NG — list of Nigerian banks with their transfer codes."""
    return _request('GET', f'/banks/{country}')


def resolve_account(account_number: str, bank_code: str) -> dict:
    """POST /accounts/resolve — look up the account name for a bank + number."""
    return _request(
        'POST',
        '/accounts/resolve',
        json={'account_number': account_number, 'account_bank': bank_code},
    )
