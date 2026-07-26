"""Thin client for the Dojah identity verification API (KYC).

Every call is server-side only — DOJAH_SECRET_KEY never reaches the app.
Docs: https://docs.dojah.io/overview/quickstart

Auth is two static headers (AppId + Authorization: raw secret key, NOT
"Bearer <key>") — same "static credential, no login dance" shape as
Quidax. Calls retry on 429/5xx with exponential backoff (max 3 attempts),
same as crypto/quidax.py, since a network blip shouldn't fail a user's
verification outright.
"""

from __future__ import annotations

import time

import requests
from django.conf import settings

_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class DojahError(Exception):
    """Raised when Dojah returns a non-2xx response or a network error."""

    def __init__(self, message: str, payload: dict | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}
        self.status_code = status_code


def _headers() -> dict:
    return {
        'AppId': settings.DOJAH_APP_ID,
        'Authorization': settings.DOJAH_SECRET_KEY,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }


def _parse(response: requests.Response) -> dict:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise DojahError(
            'Dojah returned a non-JSON response.', status_code=response.status_code
        ) from exc


def _request(method: str, path: str, **kwargs) -> dict:
    url = f'{settings.DOJAH_BASE_URL}{path}'
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
            message = payload.get('error') or payload.get('message') if isinstance(payload, dict) else None
            raise DojahError(
                message or f'Dojah returned {response.status_code}.',
                payload if isinstance(payload, dict) else {},
                status_code=response.status_code,
            )
        return payload

    raise DojahError(f'Could not reach Dojah: {last_exc}')


# ─── KYC lookups ─────────────────────────────────────────────────────────────


def verify_nin_with_selfie(*, nin: str, selfie_base64: str) -> dict:
    """POST /api/v1/kyc/nin/verify — NIN lookup + face match against the
    supplied selfie. Response includes entity data and a selfie_verification
    block with a confidence_value used to decide match (see kyc/services.py
    for the 0-90 / 90-100 threshold)."""
    return _request(
        'POST',
        '/api/v1/kyc/nin/verify',
        json={'nin': nin, 'selfie_image': selfie_base64},
    )


def verify_bvn_with_selfie(*, bvn: str, selfie_base64: str) -> dict:
    """POST /api/v1/kyc/bvn/verify — same shape as the NIN verify, for BVN."""
    return _request(
        'POST',
        '/api/v1/kyc/bvn/verify',
        json={'bvn': bvn, 'selfie_image': selfie_base64},
    )
