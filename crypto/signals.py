"""Provisions a Quidax sub-account the moment a user is created.

Eager instead of lazy (contrast with get_or_create_sub_account's original
call site — the first deposit-address request) — this way the sub-account
usually already exists by the time a user reaches crypto, and a Quidax
failure surfaces in the server log at signup time instead of as a
confusing error deep in the deposit flow. Still never blocks signup: any
failure here is caught and logged, nothing more. get_or_create_sub_account
itself is idempotent (checks for an existing row first), so this and the
lazy fallback in deposits.get_deposit_address can't double-provision.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import User

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def provision_quidax_subaccount(sender, instance: User, created: bool, **kwargs) -> None:
    if not created or not settings.QUIDAX_SECRET_KEY:
        return

    # Local import: crypto.deposits pulls in crypto.orders at module load
    # time, and signals.py is itself imported from crypto/apps.py:ready() —
    # importing at call time instead of module level keeps that import
    # order irrelevant.
    from .deposits import get_or_create_sub_account
    from .services import CryptoServiceError

    try:
        get_or_create_sub_account(instance)
    except CryptoServiceError as exc:
        logger.error('Quidax sub-account provisioning failed for %s: %s', instance.email, exc.message)
    except Exception:
        logger.exception('Unexpected error provisioning Quidax sub-account for %s', instance.email)
