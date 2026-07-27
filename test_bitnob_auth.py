"""One-off script to test Bitnob HMAC auth directly, bypassing Django.

Run on a machine with real internet access:
    python test_bitnob_auth.py

Fill in your real CLIENT_ID/CLIENT_SECRET below (or set them as env vars)
before running. Delete this file when done — it's not meant to be
committed with real credentials in it.
"""

import hashlib
import hmac
import json
import os
import secrets
import time

import requests

CLIENT_ID = os.environ.get('BITNOB_CLIENT_ID', '56753922-75b3-4403-bbd8-96f6b83c24d5')
CLIENT_SECRET = os.environ.get('BITNOB_CLIENT_SECRET', 'live_6532e93062f8d140f729c6839b27c6e1b97d8891d68695fa919d992ea0ad83f6')
BASE_URL = os.environ.get('BITNOB_BASE_URL', 'https://api.bitnob.com')


def sign(payload: str) -> dict:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    string_to_sign = f'{CLIENT_ID}:{timestamp}:{nonce}:{payload}'
    signature = hmac.new(CLIENT_SECRET.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        'X-Auth-Client': CLIENT_ID,
        'X-Auth-Timestamp': timestamp,
        'X-Auth-Nonce': nonce,
        'X-Auth-Signature': signature,
    }


def main():
    # Test 1: simplest possible authenticated GET, no body.
    url = f'{BASE_URL}/api/cards'
    headers = {**sign(''), 'Accept': 'application/json'}
    print(f'GET {url}')
    print('Headers:', json.dumps(headers, indent=2))
    response = requests.get(url, headers=headers, timeout=30)
    print(f'\nStatus: {response.status_code}')
    print('Response headers:', dict(response.headers))
    print('Body:', response.text[:2000] or '(empty)')

    # Test 2: the actual failing call — POST /api/cards (create card).
    print('\n' + '=' * 60)
    body = {
        'card_type': 'virtual',
        'currency': 'USD',
        'amount': 0,
        'name': 'Test User',
        'reference': f'TEST-{int(time.time())}',
        'customer': {
            'customer_type': 'individual',
            'first_name': 'Test',
            'last_name': 'User',
            'email': 'test.user@example.com',
            'phone_number': '8012345678',
            'dial_code': '+234',
            'date_of_birth': '1990-01-01',
            'id_type': 'national_id',
            'id_number': 'TEST12345678',
            'line1': 'Not provided',
            'city': 'Lagos',
            'state': 'Lagos',
            'postal_code': '100001',
            'country': 'NGA',
        },
    }
    payload = json.dumps(body, separators=(',', ':'))
    url2 = f'{BASE_URL}/api/cards'
    headers2 = {**sign(payload), 'Accept': 'application/json', 'Content-Type': 'application/json'}
    print(f'POST {url2}')
    print('Headers:', json.dumps(headers2, indent=2))
    print('Body:', payload)
    response2 = requests.post(url2, headers=headers2, data=payload, timeout=30)
    print(f'\nStatus: {response2.status_code}')
    print('Response headers:', dict(response2.headers))
    print('Body:', response2.text[:3000] or '(empty)')


if __name__ == '__main__':
    main()
