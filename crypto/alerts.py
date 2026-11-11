"""Ops alerts for money that isn't flowing on its own: a deposit whose
webhook never arrived, a deposit too old to credit automatically, a failed
settlement, a rejected webhook. Each is logged at ERROR and emailed to
OPS_ALERT_EMAILS, so the team hears about it before a customer does.

Deduped per process by `key` for `cooldown`, so one broken webhook doesn't
send an email per deposit per pass. A restart can repeat an alert once —
better than missing one.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}


def send_ops_alert(subject: str, body: str, *, key: str, cooldown: timedelta = timedelta(hours=6)) -> bool:
    """Log and email one alert unless `key` fired within `cooldown`.
    Never raises — an alert failing must not break the worker or a webhook.
    Returns True if the alert went out (logged, and emailed if configured)."""
    now = time.monotonic()
    last = _last_sent.get(key)
    if last is not None and now - last < cooldown.total_seconds():
        return False
    _last_sent[key] = now

    logger.error('OPS ALERT: %s — %s', subject, body)
    recipients = list(getattr(settings, 'OPS_ALERT_EMAILS', []) or [])
    if not recipients:
        logger.warning('OPS_ALERT_EMAILS is not set — alert "%s" was only logged.', subject)
        return True
    try:
        send_mail(f'[CrownEx alert] {subject}', body, settings.DEFAULT_FROM_EMAIL, recipients)
    except Exception:
        logger.exception('Could not email ops alert "%s".', subject)
    return True
