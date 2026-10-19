"""Ask Flutterwave for the real status of NGN withdrawals still pending —
for transfers whose transfer.completed webhook never reached us. The
deposit-settlement worker also runs this once a minute.

    python manage.py reconcile_withdrawals          # pending > 2 minutes
    python manage.py reconcile_withdrawals --all    # every pending one
"""

from datetime import timedelta

from django.core.management.base import BaseCommand

from wallet.models import Transaction, TransactionStatus, TransactionType
from wallet.services import RECONCILE_MIN_AGE, reconcile_pending_withdrawals


class Command(BaseCommand):
    help = 'Sync pending NGN withdrawals with their Flutterwave transfer status.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Include withdrawals made in the last 2 minutes.')

    def handle(self, *args, **options):
        min_age = timedelta(0) if options['all'] else RECONCILE_MIN_AGE
        changed = reconcile_pending_withdrawals(min_age=min_age)
        for tx, before in changed:
            self.stdout.write(f'{tx.reference} ₦{tx.amount}: {before} -> {tx.status}')
        still = Transaction.objects.filter(
            tx_type=TransactionType.WITHDRAWAL, status=TransactionStatus.PENDING
        ).count()
        self.stdout.write(f'Updated {len(changed)} withdrawal(s); {still} still pending.')
