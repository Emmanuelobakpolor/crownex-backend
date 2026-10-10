"""Worker for deposit settlements: sweeps each credited deposit from the
user's Quidax sub-account to the master account and, for auto-convert
deposits, sells it to NGN. See crypto/settlements.py.

    python manage.py process_deposit_settlements            # one pass
    python manage.py process_deposit_settlements --loop     # Procfile worker
    python manage.py process_deposit_settlements --list-failed
    python manage.py process_deposit_settlements --retry 42

--retry resumes a FAILED settlement. Check the Quidax dashboard first:
a settlement that failed with "outcome unknown" may already have swept or
sold on Quidax, and retrying it would do it again.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from crypto import orders, settlements
from crypto.models import CryptoDepositSettlement, SettlementStatus
from crypto.services import CryptoServiceError


class Command(BaseCommand):
    help = 'Sweep credited crypto deposits to the master account and run auto-conversions.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true', help='Keep running (worker process).')
        parser.add_argument('--interval', type=float, default=15, help='Seconds between passes with --loop.')
        parser.add_argument('--retry', type=int, metavar='ID', help='Resume one FAILED settlement.')
        parser.add_argument('--list-failed', action='store_true')

    def handle(self, *args, **options):
        if options['list_failed']:
            for s in CryptoDepositSettlement.objects.filter(status=SettlementStatus.FAILED).order_by('pk'):
                self.stdout.write(
                    f'{s.pk:<6} {s.user.email} {s.amount} {s.coin.upper()} '
                    f'auto_convert={s.auto_convert} swept={bool(s.swept_at)} '
                    f'sweep_ref={s.sweep_reference or "-"} — {s.last_error}'
                )
            return

        if options['retry']:
            s = CryptoDepositSettlement.objects.filter(pk=options['retry']).first()
            if s is None:
                raise CommandError(f'No settlement {options["retry"]}.')
            try:
                s = settlements.admin_retry(s)
            except CryptoServiceError as exc:
                raise CommandError(exc.message)
            s = settlements.process_settlement(s.pk)
            self.stdout.write(f'Settlement {s.pk} is now {s.status}.')
            return

        last_swap_check = 0.0
        while True:
            close_old_connections()
            processed = settlements.process_due()
            if processed:
                self.stdout.write(f'Processed {processed} settlement(s).')
            # Instant swaps whose outcome never reached us — once a minute.
            if time.monotonic() - last_swap_check >= 60:
                last_swap_check = time.monotonic()
                for order, before in orders.reconcile_due_instant_swaps():
                    if order.status != before:
                        self.stdout.write(f'Instant swap {order.reference}: {before} -> {order.status}')
            if not options['loop']:
                return
            time.sleep(options['interval'])
