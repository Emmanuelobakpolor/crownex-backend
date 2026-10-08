"""One-off: queue a sweep for every deposit credited before deposit
settlements existed, so those coins move from the users' Quidax
sub-accounts to the master account. Dry run by default.

    python manage.py backfill_deposit_sweeps                 # list only
    python manage.py backfill_deposit_sweeps --apply         # queue them
    python manage.py backfill_deposit_sweeps --email a@b.com --apply

Sweep only — nothing is ever sold, and no user balance changes: the users'
CryptoWallet balances already reflect these deposits. The worker
(process_deposit_settlements) does the actual transfers, with the same
checks as new deposits (coins must still be in the sub-account, unique
references, confirmed before it's marked done). Safe to re-run: deposits
that already have a settlement are skipped.
"""

from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from crypto import settlements


class Command(BaseCommand):
    help = 'Queue sub-account -> master sweeps for deposits credited before settlements existed.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Queue the sweeps (default is a dry run).')
        parser.add_argument('--email', help='Only this user\'s deposits.')

    def handle(self, *args, **options):
        events = settlements.deposits_without_settlement()
        if options['email']:
            events = events.filter(user__email__iexact=options['email'])

        totals: dict[str, Decimal] = defaultdict(Decimal)
        queued = 0
        for event in events:
            totals[event.coin] += event.amount
            action = 'would queue'
            if options['apply']:
                action = 'queued' if settlements.create_backfill_sweep(event) else 'already queued'
                queued += action == 'queued'
            self.stdout.write(
                f'{action:<15} deposit={event.quidax_deposit_id} {event.amount} {event.coin.upper()} '
                f'{event.user.email} ({event.created_at:%Y-%m-%d})'
            )

        if not totals:
            self.stdout.write('No deposits without a settlement.')
            return
        self.stdout.write('Totals: ' + ', '.join(f'{amount} {coin.upper()}' for coin, amount in sorted(totals.items())))
        if options['apply']:
            self.stdout.write(f'Queued {queued}. The worker will sweep them on its next pass.')
        else:
            self.stdout.write('Dry run — pass --apply to queue these sweeps.')
