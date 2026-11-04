"""Django email backend that delivers through Resend's HTTP API.

Uses HTTPS instead of SMTP, so it works on hosts that block outbound SMTP
ports. Enabled automatically when RESEND_API_KEY is set (see settings.py).
"""

import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

RESEND_API_URL = 'https://api.resend.com/emails'


class ResendError(Exception):
    """Resend rejected the message or could not be reached."""


class ResendEmailBackend(BaseEmailBackend):
    def __init__(self, api_key=None, timeout=None, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = api_key or settings.RESEND_API_KEY
        self.timeout = timeout or getattr(settings, 'EMAIL_TIMEOUT', 15)

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            try:
                self._send(message)
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
                logger.exception('Resend failed to send email to %s', message.to)
        return sent

    def _send(self, message):
        payload = {
            'from': message.from_email or settings.DEFAULT_FROM_EMAIL,
            'to': list(message.to),
            'subject': message.subject,
            'text': message.body,
        }
        if message.cc:
            payload['cc'] = list(message.cc)
        if message.bcc:
            payload['bcc'] = list(message.bcc)
        if message.reply_to:
            payload['reply_to'] = list(message.reply_to)
        for content, mimetype in getattr(message, 'alternatives', []):
            if mimetype == 'text/html':
                payload['html'] = content

        try:
            res = requests.post(
                RESEND_API_URL,
                json=payload,
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ResendError(f'Could not reach Resend: {exc}') from exc

        if res.status_code >= 400:
            try:
                detail = res.json().get('message', res.text)
            except ValueError:
                detail = res.text
            raise ResendError(f'Resend returned {res.status_code}: {detail}')
