"""Provisions a Quidax sub-account as soon as the user has a real name.

Registration here is multi-step (register -> verify OTP -> complete_profile
sets full_name + password), so hooking this to User creation (created=True)
fires before full_name exists — the original version of this signal did
exactly that, and Quidax rejected every one of these calls because the
fallback ended up sending the user's email as a "name". Gating on
full_name being non-blank instead means this actually fires once, right
when complete_profile's save() gives the user a real name; every other
save (registration, later profile edits) is a cheap no-op since
get_or_create_sub_account checks for an existing row first.

Never blocks the request that triggered it: any failure here is caught
and logged, nothing more. The lazy fallback in deposits.get_deposit_address
covers anything this misses (e.g. Quidax was down at the time).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import User

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def provision_quidax_subaccount(sender, instance: User, **kwargs) -> None:
    if not settings.QUIDAX_SECRET_KEY:
        return
    if not (instance.full_name or '').strip():
        return  # still mid-registration — wait for complete_profile

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
