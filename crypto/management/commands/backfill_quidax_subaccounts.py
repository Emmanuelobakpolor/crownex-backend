"""Provision a Quidax sub-account for every existing user missing one.

Sub-accounts are normally created lazily on first deposit-address request
(see crypto/deposits.py) — this command is for provisioning ahead of time,
e.g. after enabling crypto for a user base that predates this feature.
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
