"""Deposit sweep + auto-convert (crypto/settlements.py) and the manual sell
flow it shares primitives with. Every Quidax call is faked — requests
itself is patched to fail the test if anything tries the network."""

from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from wallet.models import Wallet

from . import quidax, settlements
from .deposits import handle_deposit_webhook
from .models import (
    CryptoDepositEvent,
    CryptoDepositSettlement,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoWallet,
    OrderStatus,
    QuidaxSubAccount,
    QuoteType,
    SettlementStatus,
)
from .orders import admin_resolve_unknown_order, place_buy_order, place_sell_order, place_swap_order
from .quidax import QuidaxError
from .services import CryptoServiceError, create_quote

MASTER_ID = 'master-1'
RATE = Decimal('1500')  # NGN per USDT


class FakeQuidax:
    """In-memory stand-in for the endpoints settlements/orders use. Queue
    an exception (or a non-done transfer status) to make the next call fail."""

    def __init__(self):
        self.balances: dict[tuple[str, str], Decimal] = {}
        self.transfers: list[dict] = []
        self.orders: list[dict] = []
        self.by_reference: dict[str, dict] = {}
        self.transfer_errors: list[Exception] = []
        self.transfer_statuses: list[str] = []
        self.sell_errors: list[Exception] = []

    def get_wallet(self, user_id, currency):
        return {'data': {'currency': currency, 'balance': str(self.balances.get((user_id, currency), 0))}}

    def create_internal_transfer(self, **kw):
        self.transfers.append(kw)
        if self.transfer_errors:
            raise self.transfer_errors.pop(0)
        status = self.transfer_statuses.pop(0) if self.transfer_statuses else 'done'
        data = {
            'id': f'wd-{len(self.transfers)}',
            'reference': kw['reference'],
            'type': 'internal_transfer',
            'currency': kw['currency'],
            'amount': kw['amount'],
            'fee': '0.0',
            'status': status,
            'reason': 'test rejection' if status == 'rejected' else None,
        }
        self.by_reference[kw['reference']] = data
        if status == 'done':
            key = (kw['from_user_id'], kw['currency'])
            self.balances[key] = self.balances.get(key, Decimal('0')) - Decimal(kw['amount'])
        return {'status': 'success', 'data': data}

    def get_withdrawal_by_reference(self, user_id, reference):
        if reference in self.by_reference:
            return {'data': self.by_reference[reference]}
        raise QuidaxError('Withdrawal not found.', status_code=404)

    def create_instant_order(self, **kw):
        """sell_errors queues outcomes for the next market orders of any
        side: an exception fails that call, None lets it succeed."""
        self.orders.append(kw)
        error = self.sell_errors.pop(0) if self.sell_errors else None
        if error:
            raise error
        return {'data': {'id': f'ord-{len(self.orders)}'}}

    def sweeps_to_master(self):
        return [t for t in self.transfers if t['to_user_id'] == MASTER_ID]


@override_settings(QUIDAX_SECRET_KEY='', QUIDAX_MASTER_ACCOUNT_ID=MASTER_ID, QUIDAX_USER_ID='me')
class SettlementTestBase(TestCase):
    def setUp(self):
        self.q = FakeQuidax()
        patches = [
            mock.patch('crypto.quidax.requests.request', side_effect=AssertionError('real Quidax call')),
            mock.patch.multiple(
                'crypto.quidax',
                get_wallet=self.q.get_wallet,
                create_internal_transfer=self.q.create_internal_transfer,
                get_withdrawal_by_reference=self.q.get_withdrawal_by_reference,
                create_instant_order=self.q.create_instant_order,
            ),
            mock.patch('crypto.services.get_coin_rate_ngn', return_value=RATE),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        CryptoFeeSettings.objects.create(fee_type='sell', flat_usd=0, percent=1)
        self.user = self._make_user('ada@example.com', 'sub-1')

    def _make_user(self, email, sub_id, *, auto_convert=False):
        user = User.objects.create_user(email=email, password='x', full_name='Ada Lovelace')
        user.auto_convert_crypto_deposits = auto_convert
        user.save()
        QuidaxSubAccount.objects.create(user=user, quidax_user_id=sub_id)
        return user

    def deposit(self, deposit_id, amount='100', coin='usdt', sub_id='sub-1'):
        """deposit.successful as Quidax sends it; the funds also land in the
        fake sub-account balance, like on Quidax."""
        key = (sub_id, coin)
        self.q.balances[key] = self.q.balances.get(key, Decimal('0')) + Decimal(amount)
        handle_deposit_webhook(
            {
                'event': 'deposit.successful',
                'data': {'id': deposit_id, 'currency': coin, 'amount': amount, 'user': {'id': sub_id}},
            }
        )
        return CryptoDepositSettlement.objects.get(deposit__quidax_deposit_id=deposit_id)

    def run_worker(self):
        return settlements.process_due()

    def make_due(self, s):
        CryptoDepositSettlement.objects.filter(pk=s.pk).update(next_attempt_at=timezone.now())

    def crypto(self, user=None, coin='usdt'):
        return CryptoWallet.objects.get(user=user or self.user, coin=coin)

    def ngn(self, user=None):
        wallet = Wallet.objects.filter(user=user or self.user).first()
        return wallet.ngn_balance if wallet else Decimal('0')

    def enable_auto_convert(self, user=None):
        user = user or self.user
        user.auto_convert_crypto_deposits = True
        user.save()


class DepositWebhookTests(SettlementTestBase):
    def test_auto_convert_off_credits_crypto_and_only_sweeps(self):
        s = self.deposit('dep-1')
        self.assertFalse(s.auto_convert)
        self.assertFalse(s.reserved)
        self.assertEqual(self.crypto().available, Decimal('100'))
        self.assertEqual(self.q.transfers, [], 'webhook must not call Quidax')

        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertEqual(len(self.q.sweeps_to_master()), 1)
        self.assertEqual(self.q.orders, [])
        self.assertEqual(self.crypto().available, Decimal('100'))
        self.assertEqual(self.ngn(), Decimal('0'))

    def test_auto_convert_on_reserves_in_webhook_without_calling_quidax(self):
        self.enable_auto_convert()
        s = self.deposit('dep-1')
        self.assertTrue(s.auto_convert)
        self.assertTrue(s.reserved)
        self.assertEqual(s.status, SettlementStatus.PENDING)
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('0'), Decimal('100')))
        self.assertEqual((self.q.transfers, self.q.orders), ([], []))

    def test_duplicate_webhook_credits_and_queues_once(self):
        self.enable_auto_convert()
        self.deposit('dep-1')
        handle_deposit_webhook(
            {'data': {'id': 'dep-1', 'currency': 'usdt', 'amount': '100', 'user': {'id': 'sub-1'}}}
        )
        self.assertEqual(CryptoDepositEvent.objects.count(), 1)
        self.assertEqual(CryptoDepositSettlement.objects.count(), 1)
        self.assertEqual(self.crypto().total, Decimal('100'))

    def test_master_account_deposit_is_ignored(self):
        handle_deposit_webhook(
            {'data': {'id': 'dep-m', 'currency': 'usdt', 'amount': '100', 'user': {'id': MASTER_ID}}}
        )
        self.assertFalse(CryptoDepositEvent.objects.exists())


class AutoConvertTests(SettlementTestBase):
    def setUp(self):
        super().setUp()
        self.enable_auto_convert()

    def test_sweep_then_sell_credits_ngn_once(self):
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()

        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertFalse(s.reserved)
        self.assertIsNotNone(s.swept_at)
        # 100 USDT * 1500 = 150,000 minus the normal 1% sell fee.
        self.assertEqual(self.ngn(), Decimal('148500.00'))
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('0'), Decimal('0')))

        order = s.order
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.order_type, QuoteType.SELL)
        self.assertEqual((order.rate_ngn, order.fee_ngn), (RATE, Decimal('1500.00')))
        self.assertEqual(len(self.q.transfers), 1)
        self.assertEqual(len(self.q.orders), 1)
        self.assertEqual(self.q.orders[0]['market'], 'usdtngn')
        self.assertEqual(self.q.orders[0]['user_id'], 'me')
        self.assertFalse(self.q.orders[0]['retry'], 'auto-convert sells must not blind-retry')

        events = list(s.logs.values_list('event', flat=True))
        for expected in ('auto_convert_started', 'sweep_started', 'sweep_completed', 'sell_started', 'sell_completed', 'auto_convert_completed'):
            self.assertIn(expected, events)

    def test_sweep_source_and_destination(self):
        s = self.deposit('dep-1', amount='12.5')
        self.run_worker()
        transfer = self.q.transfers[0]
        self.assertEqual(transfer['from_user_id'], 'sub-1')
        self.assertEqual(transfer['to_user_id'], MASTER_ID)
        self.assertEqual(transfer['currency'], 'usdt')
        self.assertEqual(transfer['amount'], '12.5')
        self.assertEqual(transfer['reference'], f'CRYSWP-{s.pk}-1')

    def test_no_sweep_until_funds_settled_in_sub_account(self):
        s = self.deposit('dep-1')
        self.q.balances[('sub-1', 'usdt')] = Decimal('40')
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.PENDING)
        self.assertEqual(self.q.transfers, [])
        self.assertEqual(s.attempts, 1)

    def test_sweep_rejected_does_not_sell_or_debit(self):
        self.q.transfer_statuses = ['rejected']
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()

        self.assertEqual(s.status, SettlementStatus.PENDING)
        self.assertEqual(s.attempts, 1)
        self.assertIn('test rejection', s.last_error)
        self.assertGreater(s.next_attempt_at, timezone.now())
        self.assertEqual(self.q.orders, [])
        self.assertEqual(self.crypto().reserved, Decimal('100'), 'still held, not debited')
        self.assertEqual(self.ngn(), Decimal('0'))
        self.assertIn('sweep_failed', s.logs.values_list('event', flat=True))

    def test_sweep_http_rejection_confirmed_absent_retries_later(self):
        self.q.transfer_errors = [QuidaxError('Insufficient balance', status_code=422)]
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.PENDING)
        self.assertEqual(self.q.orders, [])

    def test_retry_after_failed_sweep_uses_new_reference(self):
        self.q.transfer_statuses = ['rejected']
        s = self.deposit('dep-1')
        self.run_worker()
        self.make_due(s)
        self.run_worker()
        s.refresh_from_db()

        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertEqual([t['reference'] for t in self.q.transfers], [f'CRYSWP-{s.pk}-1', f'CRYSWP-{s.pk}-2'])
        self.assertEqual(len(self.q.orders), 1)
        self.assertEqual(self.ngn(), Decimal('148500.00'))

    def test_ambiguous_sweep_error_is_resolved_by_reference_not_resent(self):
        # Quidax accepted it, but our request timed out: look it up instead.
        def accept_then_time_out(**kw):
            FakeQuidax.create_internal_transfer(self.q, **kw)
            raise QuidaxError('Could not reach Quidax: timeout')

        with mock.patch('crypto.quidax.create_internal_transfer', side_effect=accept_then_time_out):
            s = self.deposit('dep-1')
            self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertEqual(len(self.q.transfers), 1)
        self.assertEqual(self.ngn(), Decimal('148500.00'))

    def test_ambiguous_sweep_never_found_fails_without_resending(self):
        self.q.transfer_errors = [QuidaxError('Could not reach Quidax: timeout')]
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.SWEEPING)

        CryptoDepositSettlement.objects.filter(pk=s.pk).update(
            step_started_at=timezone.now() - settlements.SWEEP_LOOKUP_GRACE * 2
        )
        self.make_due(s)
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.FAILED)
        self.assertEqual(len(self.q.transfers), 1)
        self.assertFalse(s.reserved)
        self.assertEqual(self.crypto().available, Decimal('100'), 'user keeps the crypto')

    def test_sweep_processing_waits_for_webhook_before_selling(self):
        self.q.transfer_statuses = ['processing']
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.SWEEPING)
        self.assertEqual(self.q.orders, [], 'no sell before the sweep is done')

        handled = settlements.handle_sweep_webhook(
            {'data': {'id': 'wd-1', 'reference': s.sweep_reference, 'currency': 'usdt', 'amount': '100'}},
            rejected=False,
        )
        self.assertTrue(handled)
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.SWEPT)
        self.assertEqual(self.q.orders, [], 'webhook only records; the worker sells')

        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.COMPLETED)

    def test_sell_fails_after_sweep_keeps_swept_state(self):
        self.q.sell_errors = [QuidaxError('Market unavailable', status_code=422)]
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()

        self.assertEqual(s.status, SettlementStatus.SWEPT)
        self.assertIsNotNone(s.swept_at)
        self.assertTrue(s.reserved)
        self.assertEqual(self.crypto().reserved, Decimal('100'))
        self.assertEqual(self.ngn(), Decimal('0'))
        self.assertEqual(s.order.status, OrderStatus.FAILED)
        self.assertIn('sell_failed', s.logs.values_list('event', flat=True))

    def test_retry_after_sell_failure_sells_without_resweeping(self):
        self.q.sell_errors = [QuidaxError('Market unavailable', status_code=422)]
        s = self.deposit('dep-1')
        self.run_worker()
        self.make_due(s)
        self.run_worker()
        s.refresh_from_db()

        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertEqual(len(self.q.transfers), 1, 'never swept twice')
        self.assertEqual(len(self.q.orders), 2)
        self.assertEqual(self.ngn(), Decimal('148500.00'))
        self.assertEqual(self.crypto().total, Decimal('0'))
        self.assertEqual(CryptoOrder.objects.filter(status=OrderStatus.COMPLETED).count(), 1)

    def test_unknown_sell_outcome_is_never_auto_retried(self):
        self.q.sell_errors = [QuidaxError('Quidax returned 502.', status_code=502)]
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.FAILED)
        self.assertTrue(s.reserved, 'held until an admin checks Quidax')

        self.make_due(s)
        self.run_worker()
        self.assertEqual(len(self.q.orders), 1)
        self.assertEqual(self.ngn(), Decimal('0'))

    def test_duplicate_worker_runs_do_nothing_twice(self):
        s = self.deposit('dep-1')
        stale_copy = CryptoDepositSettlement.objects.get(pk=s.pk)
        self.run_worker()
        self.make_due(s)
        self.run_worker()
        settlements.process_settlement(s.pk)
        # A second worker still holding the PENDING row it loaded earlier —
        # with enough balance on Quidax that only the status check stops it.
        self.q.balances[('sub-1', 'usdt')] = Decimal('1000')
        settlements._start_sweep(stale_copy)

        self.assertEqual(len(self.q.transfers), 1)
        self.assertEqual(len(self.q.orders), 1)
        self.assertEqual(self.ngn(), Decimal('148500.00'))
        self.assertEqual(self.crypto().total, Decimal('0'))

    def test_stale_sell_claim_cannot_sell_again(self):
        s = self.deposit('dep-1')
        self.run_worker()
        stale = CryptoDepositSettlement.objects.get(pk=s.pk)
        stale.status = SettlementStatus.SWEPT  # what a slow worker last saw
        settlements._after_sweep(stale)
        self.assertEqual(len(self.q.orders), 1)
        self.assertEqual(self.ngn(), Decimal('148500.00'))

    def test_below_minimum_is_skipped_and_crypto_kept(self):
        s = self.deposit('dep-1', amount='0.5')  # 0.5 * 1500 = ₦750 < ₦1000
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.SKIPPED)
        self.assertEqual(len(self.q.transfers), 1, 'still swept to master')
        self.assertEqual(self.q.orders, [])
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('0.5'), Decimal('0')))
        self.assertEqual(self.ngn(), Decimal('0'))
        self.assertIn('auto_convert_skipped', s.logs.values_list('event', flat=True))

    def test_coin_without_ngn_market_is_skipped(self):
        s = self.deposit('dep-1', amount='3', coin='bnb')
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.SKIPPED)
        self.assertEqual(self.q.orders, [])
        self.assertEqual(self.crypto(coin='bnb').available, Decimal('3'))

    def test_multiple_deposits_settle_independently(self):
        self.q.transfer_statuses = ['done', 'rejected']
        first = self.deposit('dep-1', amount='100')
        second = self.deposit('dep-2', amount='20')
        self.run_worker()
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(first.status, SettlementStatus.COMPLETED)
        self.assertEqual(second.status, SettlementStatus.PENDING)
        self.assertNotEqual(first.sweep_reference, second.sweep_reference)
        self.assertEqual(self.ngn(), Decimal('148500.00'))
        self.assertEqual(self.crypto().reserved, Decimal('20'))

        self.make_due(second)
        self.run_worker()
        second.refresh_from_db()
        self.assertEqual(second.status, SettlementStatus.COMPLETED)
        # + 20 * 1500 = 30,000 - 1% fee = 29,700
        self.assertEqual(self.ngn(), Decimal('178200.00'))
        self.assertEqual(self.crypto().total, Decimal('0'))

    def test_other_users_deposits_use_their_own_sub_account(self):
        bola = self._make_user('bola@example.com', 'sub-2', auto_convert=True)
        self.deposit('dep-1', amount='100')
        self.deposit('dep-2', amount='10', sub_id='sub-2')
        self.run_worker()
        sources = sorted((t['from_user_id'], t['amount']) for t in self.q.transfers)
        self.assertEqual(sources, [('sub-1', '100'), ('sub-2', '10')])
        self.assertEqual(self.ngn(bola), Decimal('14850.00'))
        self.assertEqual(self.ngn(), Decimal('148500.00'))

    def test_admin_retry_after_failed_sweep(self):
        self.q.transfer_errors = [QuidaxError('Could not reach Quidax: timeout')]
        s = self.deposit('dep-1')
        self.run_worker()
        CryptoDepositSettlement.objects.filter(pk=s.pk).update(
            step_started_at=timezone.now() - settlements.SWEEP_LOOKUP_GRACE * 2
        )
        self.make_due(s)
        self.run_worker()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.FAILED)

        settlements.admin_retry(s)
        s = settlements.process_settlement(s.pk)
        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertEqual(s.sweep_reference, f'CRYSWP-{s.pk}-2')
        self.assertEqual(self.ngn(), Decimal('148500.00'))


@override_settings(QUIDAX_SECRET_KEY='', QUIDAX_USER_ID='me')
class ManualSellTests(SettlementTestBase):
    def test_manual_sell_unchanged(self):
        self.user.set_transaction_pin('1234')
        self.user.save()
        CryptoWallet.objects.create(user=self.user, coin='usdt', available=Decimal('10'))

        quote = create_quote(self.user, quote_type=QuoteType.SELL, coin='usdt', amount=Decimal('10'))
        order = place_sell_order(self.user, quote_id=quote.id, pin='1234')

        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.ngn(), Decimal('14850.00'))
        self.assertEqual(self.crypto().total, Decimal('0'))
        self.assertEqual(self.q.orders[0]['user_id'], 'me')
        self.assertFalse(self.q.orders[0]['retry'], 'no blind retry of market orders')

    def test_manual_sell_failure_releases_reservation(self):
        self.user.set_transaction_pin('1234')
        self.user.save()
        CryptoWallet.objects.create(user=self.user, coin='usdt', available=Decimal('10'))
        self.q.sell_errors = [QuidaxError('Market unavailable', status_code=422)]

        quote = create_quote(self.user, quote_type=QuoteType.SELL, coin='usdt', amount=Decimal('10'))
        order = place_sell_order(self.user, quote_id=quote.id, pin='1234')

        self.assertEqual(order.status, OrderStatus.FAILED)
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('10'), Decimal('0')))


class WebhookRoutingTests(SettlementTestBase):
    def _post(self, payload):
        with mock.patch('crypto.views._quidax_signature_valid', return_value=True), override_settings(
            QUIDAX_WEBHOOK_SECRET='test'
        ):
            return APIClient().post('/api/crypto/webhook/quidax/', payload, format='json')

    def test_sweep_withdraw_webhook_is_routed_to_settlement(self):
        self.enable_auto_convert()
        self.q.transfer_statuses = ['processing']
        s = self.deposit('dep-1')
        self.run_worker()
        s.refresh_from_db()

        with mock.patch('crypto.withdrawals.handle_withdraw_webhook') as regular:
            response = self._post(
                {
                    'event': 'withdraw.successful',
                    'data': {'id': 'wd-1', 'reference': s.sweep_reference, 'currency': 'usdt', 'amount': '100'},
                }
            )
        self.assertEqual(response.status_code, 200)
        regular.assert_not_called()
        s.refresh_from_db()
        self.assertEqual(s.status, SettlementStatus.SWEPT)

    def test_regular_withdraw_webhook_still_reaches_withdrawals(self):
        with mock.patch('crypto.withdrawals.handle_withdraw_webhook') as regular:
            self._post({'event': 'withdraw.successful', 'data': {'id': 'x', 'reference': 'CRYWD-1-abc'}})
        regular.assert_called_once()


class CryptoSettingsEndpointTests(SettlementTestBase):
    def test_toggle_auto_convert(self):
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertEqual(client.get('/api/crypto/settings/').json(), {'auto_convert_deposits': False})
        response = client.patch('/api/crypto/settings/', {'auto_convert_deposits': True}, format='json')
        self.assertEqual(response.json(), {'auto_convert_deposits': True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.auto_convert_crypto_deposits)


class BackfillSweepTests(SettlementTestBase):
    def _old_deposit(self, deposit_id, amount='40'):
        """A deposit credited before settlements existed: event + balance, no settlement."""
        event = CryptoDepositEvent.objects.create(
            quidax_deposit_id=deposit_id, user=self.user, coin='usdt', amount=Decimal(amount)
        )
        CryptoWallet.objects.update_or_create(
            user=self.user, coin='usdt', defaults={'available': Decimal(amount)}
        )
        self.q.balances[('sub-1', 'usdt')] = Decimal(amount)
        return event

    def test_dry_run_changes_nothing(self):
        self._old_deposit('old-1')
        call_command('backfill_deposit_sweeps', stdout=StringIO())
        self.assertFalse(CryptoDepositSettlement.objects.exists())

    def test_apply_sweeps_only_never_converts(self):
        self.enable_auto_convert()  # must not apply to old deposits
        self._old_deposit('old-1')
        call_command('backfill_deposit_sweeps', '--apply', stdout=StringIO())
        call_command('backfill_deposit_sweeps', '--apply', stdout=StringIO())  # re-run is a no-op
        self.assertEqual(CryptoDepositSettlement.objects.count(), 1)

        self.run_worker()
        s = CryptoDepositSettlement.objects.get()
        self.assertEqual(s.status, SettlementStatus.COMPLETED)
        self.assertFalse(s.auto_convert)
        self.assertEqual(len(self.q.sweeps_to_master()), 1)
        self.assertEqual(self.q.orders, [])
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('40'), Decimal('0')))
        self.assertEqual(self.ngn(), Decimal('0'))

    def test_coins_no_longer_in_sub_account_are_not_swept(self):
        self._old_deposit('old-1')
        self.q.balances[('sub-1', 'usdt')] = Decimal('0')
        call_command('backfill_deposit_sweeps', '--apply', stdout=StringIO())
        self.run_worker()
        s = CryptoDepositSettlement.objects.get()
        self.assertEqual(s.status, SettlementStatus.PENDING)
        self.assertEqual(self.q.transfers, [])


class UnknownOutcomeTests(SettlementTestBase):
    """Manual buy/sell/swap: a timeout or 5xx parks the order for an admin
    instead of refunding/releasing (Quidax may have executed it)."""

    def setUp(self):
        super().setUp()
        self.user.set_transaction_pin('1234')
        self.user.save()

    def _sell(self, amount='10'):
        CryptoWallet.objects.update_or_create(user=self.user, coin='usdt', defaults={'available': Decimal(amount)})
        quote = create_quote(self.user, quote_type=QuoteType.SELL, coin='usdt', amount=Decimal(amount))
        return place_sell_order(self.user, quote_id=quote.id, pin='1234')

    def _buy(self):
        Wallet.objects.update_or_create(user=self.user, defaults={'ngn_balance': Decimal('20000')})
        quote = create_quote(self.user, quote_type=QuoteType.BUY, coin='usdt', amount=Decimal('10'))
        return place_buy_order(self.user, quote_id=quote.id, pin='1234')

    def _swap(self):
        CryptoWallet.objects.update_or_create(user=self.user, coin='usdt', defaults={'available': Decimal('10')})
        quote = create_quote(
            self.user, quote_type=QuoteType.SWAP, coin='usdt', amount=Decimal('10'), to_coin='btc'
        )
        return place_swap_order(self.user, quote_id=quote.id, pin='1234')

    def test_sell_timeout_holds_crypto_for_review(self):
        self.q.sell_errors = [QuidaxError('Could not reach Quidax: timeout')]
        order = self._sell()
        self.assertEqual(order.status, OrderStatus.PROCESSING)
        self.assertTrue(order.needs_review)
        self.assertEqual(len(self.q.orders), 1)
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('0'), Decimal('10')))
        self.assertEqual(self.ngn(), Decimal('0'))

    def test_sell_resolved_executed_pays_once(self):
        self.q.sell_errors = [QuidaxError('Quidax returned 502.', status_code=502)]
        order = admin_resolve_unknown_order(self._sell(), 'seen on Quidax', executed=True)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertFalse(order.needs_review)
        self.assertEqual(self.ngn(), Decimal('14850.00'))
        self.assertEqual(self.crypto().total, Decimal('0'))
        with self.assertRaises(CryptoServiceError):
            admin_resolve_unknown_order(order, executed=True)
        self.assertEqual(self.ngn(), Decimal('14850.00'))

    def test_sell_resolved_not_executed_releases(self):
        self.q.sell_errors = [QuidaxError('Quidax returned 504.', status_code=504)]
        order = admin_resolve_unknown_order(self._sell(), executed=False)
        self.assertEqual(order.status, OrderStatus.FAILED)
        wallet = self.crypto()
        self.assertEqual((wallet.available, wallet.reserved), (Decimal('10'), Decimal('0')))
        self.assertEqual(self.ngn(), Decimal('0'))

    def test_buy_timeout_does_not_refund_until_resolved(self):
        self.q.sell_errors = [QuidaxError('Could not reach Quidax: timeout')]
        order = self._buy()
        self.assertEqual(order.status, OrderStatus.PROCESSING)
        self.assertTrue(order.needs_review)
        self.assertEqual(self.ngn(), Decimal('20000') - order.total_ngn)

        order = admin_resolve_unknown_order(order, executed=False)
        self.assertEqual(order.status, OrderStatus.FAILED)
        self.assertEqual(self.ngn(), Decimal('20000'))

    def test_buy_resolved_executed_credits_crypto(self):
        self.q.sell_errors = [QuidaxError('Could not reach Quidax: timeout')]
        order = admin_resolve_unknown_order(self._buy(), executed=True)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.crypto().available, Decimal('10'))
        self.assertEqual(self.ngn(), Decimal('20000') - order.total_ngn)

    def test_buy_refused_still_refunds_immediately(self):
        self.q.sell_errors = [QuidaxError('Market closed', status_code=422)]
        order = self._buy()
        self.assertEqual(order.status, OrderStatus.FAILED)
        self.assertFalse(order.needs_review)
        self.assertEqual(self.ngn(), Decimal('20000'))

    def test_swap_sell_leg_unknown_then_executed_runs_buy_leg(self):
        self.q.sell_errors = [QuidaxError('Could not reach Quidax: timeout')]
        order = self._swap()
        self.assertEqual((order.status, order.needs_review), (OrderStatus.PROCESSING, True))
        self.assertEqual(self.crypto().reserved, Decimal('10'))

        order = admin_resolve_unknown_order(order, executed=True)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.q.orders[-1]['side'], 'buy')
        self.assertEqual(self.crypto().total, Decimal('0'))
        self.assertEqual(self.crypto(coin='btc').available, order.to_coin_amount)

    def test_swap_buy_leg_unknown_debits_source_once(self):
        self.q.sell_errors = [None, QuidaxError('Quidax returned 502.', status_code=502)]
        order = self._swap()
        self.assertEqual((order.status, order.needs_review), (OrderStatus.PROCESSING, True))
        self.assertEqual(self.crypto().total, Decimal('0'), 'sell leg done: source debited')

        order = admin_resolve_unknown_order(order, executed=True)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.crypto().total, Decimal('0'))
        self.assertEqual(self.crypto(coin='btc').available, order.to_coin_amount)

    def test_only_flagged_orders_can_be_resolved(self):
        order = self._sell()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        with self.assertRaises(CryptoServiceError):
            admin_resolve_unknown_order(order, executed=False)


class QuidaxClientRetryTests(TestCase):
    @override_settings(QUIDAX_SECRET_KEY='k')
    def test_retry_false_sends_once_on_5xx(self):
        response = mock.Mock(status_code=502, ok=False, content=b'{}')
        response.json.return_value = {}
        with mock.patch('crypto.quidax.requests.request', return_value=response) as request:
            with self.assertRaises(QuidaxError) as ctx:
                quidax.create_instant_order(market='usdtngn', side='sell', volume='1', retry=False)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(ctx.exception.status_code, 502)
