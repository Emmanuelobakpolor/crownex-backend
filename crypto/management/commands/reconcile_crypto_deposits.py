"""Credit crypto deposits whose Quidax deposit.successful webhook never
arrived (or was dropped). Dry run by default — pass --apply to credit.

    python manage.py reconcile_crypto_deposits --email user@x.com --coin usdt
    python manage.py reconcile_crypto_deposits --email user@x.com --coin usdt --apply

Idempotent: each Quidax deposit id is credited at most once (the same
CryptoDepositEvent guard the webhook uses), so re-running is safe.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from crypto.deposits import reconcile_deposits
from crypto.services import CryptoServiceError


class Command(BaseCommand):
    help = 'Reconcile a user\'s Quidax deposits against credited CryptoDepositEvents.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--coin', required=True)
        parser.add_argument('--apply', action='store_true', help='Actually credit (default is dry run).')

    def handle(self, *args, **options):
        user = User.objects.filter(email__iexact=options['email']).first()
        if not user:
            raise CommandError(f'No user with email {options["email"]}.')

        try:
            results = reconcile_deposits(user, options['coin'], apply=options['apply'])
        except CryptoServiceError as exc:
            raise CommandError(exc.message)

        if not results:
            self.stdout.write('Quidax reports no deposits for this user/coin.')
        for r in results:
            self.stdout.write(
                f'{r["action"]:<22} id={r["id"]} {r["amount"]} {r["currency"]} '
                f'state={r["state"]} txid={r["txid"]}'
            )
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run — re-run with --apply to credit.'))
