"""NGN withdrawal status sync: the Flutterwave transfer webhook and the
reconciliation that covers for it when it never arrives. Every Flutterwave
call is faked — requests itself is patched to fail on any real call."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User

from . import services
from .models import Transaction, TransactionStatus, TransactionType, Wallet


@override_settings(FLW_WEBHOOK_HASH='hash-123')
class WithdrawalStatusTests(TestCase):
    def setUp(self):
        patcher = mock.patch('wallet.flutterwave.requests.request', side_effect=AssertionError('real Flutterwave call'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(email='ada@example.com', password='x', full_name='Ada Lovelace')
        # Balance after the withdrawals below were debited.
        self.wallet = Wallet.objects.create(user=self.user, ngn_balance=Decimal('1000'))

    def withdrawal(self, reference, amount='3100', *, transfer_id='', age=timedelta(minutes=10)):
        tx = Transaction.objects.create(
            user=self.user,
            tx_type=TransactionType.WITHDRAWAL,
            amount=Decimal(amount),
            reference=reference,
            flw_transfer_id=transfer_id,
            bank_name='Moniepoint Microfinance Bank',
            account_number='6000000000',
        )
        Transaction.objects.filter(pk=tx.pk).update(created_at=timezone.now() - age)
        return tx

    def status(self, tx):
        tx.refresh_from_db()
        return tx.status

    def balance(self):
        self.wallet.refresh_from_db()
        return self.wallet.ngn_balance

    def test_successful_transfer_found_by_reference_is_completed(self):
        tx = self.withdrawal('WD-1')
        listing = {'data': [{'id': 555, 'reference': 'WD-1', 'status': 'SUCCESSFUL'}], 'meta': {'page_info': {'total_pages': 1}}}
        with mock.patch('wallet.flutterwave.list_transfers', return_value=listing):
            changed = services.reconcile_pending_withdrawals()
        self.assertEqual(self.status(tx), TransactionStatus.COMPLETED)
        self.assertEqual(tx.flw_transfer_id, '555')
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.balance(), Decimal('1000'))

    def test_failed_transfer_is_refunded(self):
        tx = self.withdrawal('WD-1', transfer_id='777')
        with mock.patch('wallet.flutterwave.get_transfer', return_value={'data': {'id': 777, 'status': 'FAILED'}}):
            services.reconcile_pending_withdrawals()
        self.assertEqual(self.status(tx), TransactionStatus.FAILED)
        self.assertEqual(self.balance(), Decimal('4100'))

    def test_still_pending_or_unknown_transfer_is_left_alone(self):
        pending = self.withdrawal('WD-1', transfer_id='777')
        unknown = self.withdrawal('WD-2')
        with mock.patch('wallet.flutterwave.get_transfer', return_value={'data': {'id': 777, 'status': 'PENDING'}}), \
                mock.patch('wallet.flutterwave.list_transfers', return_value={'data': [], 'meta': {}}):
            changed = services.reconcile_pending_withdrawals()
        self.assertEqual(changed, [])
        self.assertEqual(self.status(pending), TransactionStatus.PENDING)
        self.assertEqual(self.status(unknown), TransactionStatus.PENDING, 'never refunded without a Flutterwave record')
        self.assertEqual(self.balance(), Decimal('1000'))

    def test_recent_withdrawal_waits_for_webhook(self):
        self.withdrawal('WD-1', age=timedelta(seconds=10))
        with mock.patch('wallet.flutterwave.list_transfers') as listing:
            services.reconcile_pending_withdrawals()
        listing.assert_not_called()

    def test_webhook_completes_withdrawal(self):
        tx = self.withdrawal('WD-1')
        response = APIClient().post(
            '/api/wallet/flw-webhook/',
            {'event': 'transfer.completed', 'data': {'reference': 'WD-1', 'status': 'SUCCESSFUL'}},
            format='json',
            HTTP_VERIF_HASH='hash-123',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(tx), TransactionStatus.COMPLETED)

    def test_webhook_with_wrong_hash_is_rejected_and_logged(self):
        tx = self.withdrawal('WD-1')
        with self.assertLogs('wallet.views', level='WARNING'):
            response = APIClient().post(
                '/api/wallet/flw-webhook/',
                {'event': 'transfer.completed', 'data': {'reference': 'WD-1', 'status': 'SUCCESSFUL'}},
                format='json',
                HTTP_VERIF_HASH='wrong',
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.status(tx), TransactionStatus.PENDING)
