"""Answers the open questions about Quidax Instant Swap before trading is
moved onto it. Run where QUIDAX_SECRET_KEY is set (e.g. a one-off dyno).

    python manage.py probe_quidax_swap
        Price preview only (temporary_swap_quotation — nothing to confirm,
        no funds move). For every supported coin, asks Quidax to price
        ~₦5,000 of coin -> NGN and NGN -> coin, and reports which pairs it
        quotes and how its price compares to the ticker rate the app uses.

    python manage.py probe_quidax_swap --execute usdt 2
        REAL trade on the master account: quotes and confirms a swap of
        2 USDT -> NGN, waits for it to finish, then compares the confirmed
        execution_price / received_amount with the quote. Asks you to type
        the amount back before doing it.
"""

import time
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from crypto import quidax
from crypto.quidax import QuidaxError
from crypto.services import SUPPORTED_COINS, CryptoServiceError, get_coin_rate_ngn

PROBE_NGN = Decimal('5000')


def _data(payload) -> dict:
    data = payload.get('data') if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


class Command(BaseCommand):
    help = 'Probe Quidax Instant Swap pair support and quote accuracy.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', nargs=2, metavar=('COIN', 'AMOUNT'), help='Run one REAL coin -> NGN swap.')
        parser.add_argument('--no-input', action='store_true', help='Skip the typed confirmation for --execute.')

    def handle(self, *args, **options):
        if options['execute']:
            coin, amount = options['execute']
            self._execute(coin.lower(), Decimal(amount), ask=not options['no_input'])
        else:
            self._preview_all()

    # ── price preview (no funds move) ──────────────────────────────────────

    def _preview_all(self):
        self.stdout.write(f'{"coin":<6} {"direction":<11} {"result":<8} {"swap price":>16} {"app rate":>16}  detail')
        for coin in SUPPORTED_COINS:
            try:
                rate = get_coin_rate_ngn(coin)
            except CryptoServiceError as exc:
                self.stdout.write(f'{coin:<6} {"-":<11} {"NO RATE":<8} {"":>16} {"":>16}  {exc.message}')
                continue
            coin_amount = (PROBE_NGN / rate).quantize(Decimal('0.00000001'))
            self._preview(coin, 'ngn', str(coin_amount), rate, f'{coin}->ngn')
            self._preview('ngn', coin, str(PROBE_NGN), rate, f'ngn->{coin}')

    def _preview(self, from_currency, to_currency, from_amount, app_rate, label):
        coin = from_currency if to_currency == 'ngn' else to_currency
        try:
            payload = quidax.temporary_swap_quotation(
                from_currency=from_currency, to_currency=to_currency, from_amount=from_amount
            )
        except QuidaxError as exc:
            self.stdout.write(f'{coin:<6} {label:<11} {"FAIL":<8} {"":>16} {"":>16}  {exc.status_code} {exc.message}')
            return
        data = _data(payload)
        price = data.get('quoted_price')
        self.stdout.write(
            f'{coin:<6} {label:<11} {"OK":<8} {str(price):>16} {str(app_rate.quantize(Decimal("0.01"))):>16}  '
            f'to_amount={data.get("to_amount")} quoted_currency={data.get("quoted_currency")} keys={sorted(data)}'
        )

    # ── one real swap ──────────────────────────────────────────────────────

    def _execute(self, coin, amount, *, ask):
        if coin not in SUPPORTED_COINS:
            raise CommandError(f'Unsupported coin {coin}.')
        if ask:
            self.stdout.write(self.style.WARNING(
                f'This SELLS {amount} {coin.upper()} from the Quidax MASTER account for NGN, for real.'
            ))
            if input(f'Type {amount} to continue: ').strip() != str(amount):
                raise CommandError('Cancelled.')

        try:
            quote = _data(quidax.create_swap_quotation(from_currency=coin, to_currency='ngn', from_amount=str(amount)))
        except QuidaxError as exc:
            raise CommandError(f'Quotation failed: {exc.status_code} {exc.message}')
        self.stdout.write(f'Quote: {quote}')

        try:
            confirmed = _data(quidax.confirm_swap_quotation(str(quote['id'])))
        except QuidaxError as exc:
            raise CommandError(f'Confirm failed (check the Quidax dashboard before retrying): {exc.status_code} {exc.message}')
        self.stdout.write(f'Confirm response: {confirmed}')

        swap = confirmed
        for _ in range(20):
            if str(swap.get('status', '')).lower() not in ('initiated', 'pending', 'processing', ''):
                break
            time.sleep(3)
            swap = _data(quidax.get_swap_transaction(str(confirmed['id'])))
        self.stdout.write(f'Final: {swap}')

        quoted_price = Decimal(str(quote.get('quoted_price') or 0))
        to_amount = Decimal(str(quote.get('to_amount') or 0))
        received = Decimal(str(swap.get('received_amount') or 0))
        self.stdout.write('')
        self.stdout.write(f'status            {swap.get("status")}')
        self.stdout.write(f'quoted_price      {quoted_price}')
        self.stdout.write(f'execution_price   {swap.get("execution_price")}')
        self.stdout.write(f'quoted to_amount  {to_amount}')
        self.stdout.write(f'received_amount   {received}')
        self.stdout.write(f'difference        {received - to_amount}')
