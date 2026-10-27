"""Show what our DB and Reloadly each say about pending gift card
purchases, then apply Reloadly's answer (complete with code, or refund).

    python manage.py requery_giftcard GCBUY-...          # one purchase
    python manage.py requery_giftcard --all              # every pending one
    python manage.py requery_giftcard GCBUY-... --dry-run
"""

import json

from django.core.management.base import BaseCommand, CommandError

from giftcards import reloadly, services
from giftcards.models import GiftCardPurchase, GiftCardStatus
from giftcards.reloadly import ReloadlyError


class Command(BaseCommand):
    help = 'Re-check gift card purchases against Reloadly and apply the result.'

    def add_arguments(self, parser):
        parser.add_argument('reference', nargs='?', help='GCBUY-... reference')
        parser.add_argument('--all', action='store_true', help='Every pending purchase.')
        parser.add_argument('--dry-run', action='store_true', help="Print only; don't change anything.")

    def handle(self, *args, **options):
        if options['reference']:
            purchases = GiftCardPurchase.objects.filter(reference=options['reference'])
            if not purchases:
                raise CommandError(f'No purchase with reference {options["reference"]}.')
        elif options['all']:
            purchases = GiftCardPurchase.objects.filter(status=GiftCardStatus.PENDING)
        else:
            raise CommandError('Pass a reference or --all.')

        for purchase in purchases:
            self._requery(purchase, dry_run=options['dry_run'])

    def _dump(self, label, data):
        self.stdout.write(f'  {label}: {json.dumps(data, indent=2, default=str)}')

    def _requery(self, purchase, *, dry_run):
        self.stdout.write(
            f'\n{purchase.reference}  {purchase.user.email}  {purchase.brand} '
            f'${purchase.unit_price_usd} ₦{purchase.amount_ngn}  status={purchase.status} '
            f'reloadly_tx_id={purchase.reloadly_tx_id or "(none)"}  note={purchase.note!r}'
        )

        if not purchase.reloadly_tx_id:
            self.stdout.write(self.style.WARNING(
                '  No Reloadly transaction id saved — the order call never returned one. '
                'Search the Reloadly dashboard for this reference (customIdentifier) '
                'before refunding, in case the order was placed.'
            ))
            return

        try:
            order = reloadly.get_order(purchase.reloadly_tx_id)
        except ReloadlyError as exc:
            self.stdout.write(self.style.ERROR(f'  get_order failed: {exc.message}'))
            self._dump('payload', exc.payload)
            order = {'transactionId': purchase.reloadly_tx_id}
        else:
            self._dump('order', order)

        try:
            cards = reloadly.get_redeem_codes(purchase.reloadly_tx_id)
            self.stdout.write(f'  redeem cards returned: {len(cards)}')
        except ReloadlyError as exc:
            self.stdout.write(self.style.WARNING(f'  get_redeem_codes failed: {exc.message}'))
            self._dump('payload', exc.payload)

        if dry_run:
            return

        updated = services._apply_order_result(purchase, order)
        self.stdout.write(self.style.SUCCESS(f'  -> status now {updated.status}'))
