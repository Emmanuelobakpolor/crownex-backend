"""Provision a Quidax sub-account for every existing user missing one.

Sub-accounts are normally created automatically (a post_save signal on
User — see crypto/signals.py — with a lazy fallback on first
deposit-address request in crypto/deposits.py). This command is for
backfilling users who predate the feature, or whose automatic attempt
failed (e.g. Quidax was unreachable at the time).
"""

from django.core.management.base import BaseCommand

from accounts.models import User
from crypto.deposits import get_or_create_sub_account
from crypto.models import QuidaxSubAccount
from crypto.services import CryptoServiceError


class Command(BaseCommand):
    help = 'Provision a Quidax sub-account for every profile-complete user missing one.'

    def handle(self, *args, **options):
        missing = User.objects.filter(is_profile_complete=True).exclude(
            id__in=QuidaxSubAccount.objects.values_list('user_id', flat=True)
        )
        total = missing.count()
        ok = 0

        for user in missing.iterator():
            try:
                get_or_create_sub_account(user)
                ok += 1
            except CryptoServiceError as exc:
                self.stderr.write(f'{user.email}: {exc.message}')

        self.stdout.write(self.style.SUCCESS(f'Provisioned {ok}/{total} sub-accounts.'))
