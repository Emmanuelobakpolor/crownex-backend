"""Finishes instant swaps whose outcome never reached us (lost confirm
response, missed swap_transaction.* webhook, crash mid-finalize).

    python manage.py reconcile_instant_swaps            # older than 60s
    python manage.py reconcile_instant_swaps --age 0

The settlement worker (process_deposit_settlements --loop) already runs
this every minute; the command is for doing it by hand. It only applies
what Quidax reports for each order's own swap/quotation id, and never
releases or credits on a guess.
"""

from django.core.management.base import BaseCommand

from crypto.orders import reconcile_due_instant_swaps


class Command(BaseCommand):
    help = 'Apply Quidax outcomes to processing instant swaps.'

    def add_arguments(self, parser):
        parser.add_argument('--age', type=int, default=60, help='Only orders older than this many seconds.')

    def handle(self, *args, **options):
        results = reconcile_due_instant_swaps(options['age'])
        for order, before in results:
            self.stdout.write(f'{order.reference}: {before} -> {order.status}')
        if not results:
            self.stdout.write('No processing instant swaps to reconcile.')
