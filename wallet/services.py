"""Business logic for the NGN wallet: deposits, withdrawals, and webhooks.

Every balance mutation goes through `select_for_update()` inside an atomic
block, and every credit/refund path checks the transaction's current status
before acting — that's what makes verify-payment, the webhook, and a retried
client call all safe to run more than once for the same reference.

Deposits and withdrawals are both asynchronous under Flutterwave v4 (no
instant SDK-confirmed charge): a deposit sits `pending` until the user
actually completes a bank transfer or dials a USSD code, and a withdrawal
sits `pending` until Flutterwave's payout rail settles it. Because the exact
webhook event names/payloads for v4 orders and transfers aren't fully
documented, the webhook handlers here treat the webhook purely as a
"something happened, go check" trigger and always re-fetch the authoritative
status via GET before crediting or failing anything — never trust the
webhook body's status directly. This matches Flutterwave's own documented
best practice.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction

from . import flutterwave
from .flutterwave import FlutterwaveError
from .models import (
    DepositMethod,
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
    generate_withdrawal_reference,
)

MIN_DEPOSIT = Decimal('100')
MIN_WITHDRAWAL = Decimal('500')

# Flutterwave v4 order/transfer statuses that mean "done, credit/settle it".
_ORDER_COMPLETED_STATUSES = {'completed'}
_ORDER_FAILED_STATUSES = {'voided', 'failed'}
_TRANSFER_SUCCESS_STATUSES = {'SUCCESSFUL'}
_TRANSFER_FAILED_STATUSES = {'FAILED', 'CANCELLED'}


class WalletServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def get_or_create_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def _customer_payload(user) -> dict:
    parts = (user.full_name or '').strip().split(maxsplit=1)
    first, last = (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else user.email, '')
    customer = {'email': user.email, 'name': {'first': first, 'last': last}}
    if user.phone:
        customer['phone'] = {'country_code': '234', 'number': user.phone}
    return customer


# ─── Deposit ──────────────────────────────────────────────────────────────


@transaction.atomic
def initiate_deposit(
    user, amount: Decimal, tx_ref: str, method: str, bank_code: str = ''
) -> Transaction:
    if amount < MIN_DEPOSIT:
        raise WalletServiceError(
            f'Minimum deposit amount is ₦{MIN_DEPOSIT}.', code='amount_too_low'
        )
    if method not in (DepositMethod.BANK_TRANSFER, DepositMethod.USSD):
        raise WalletServiceError('Unsupported deposit method.', code='invalid_method')
    if method == DepositMethod.USSD and not bank_code:
        raise WalletServiceError(
            'Select a bank to generate a USSD code.', code='bank_code_required'
        )
    if Transaction.objects.filter(flw_tx_ref=tx_ref).exists():
        raise WalletServiceError(
            'This transaction reference has already been used.',
            code='duplicate_tx_ref',
            status=409,
        )

    get_or_create_wallet(user)

    try:
        tx = Transaction.objects.create(
            user=user,
            tx_type=TransactionType.DEPOSIT,
            amount=amount,
            status=TransactionStatus.PENDING,
            reference=tx_ref,
            flw_tx_ref=tx_ref,
            deposit_method=method,
            bank_code=bank_code,
        )
    except IntegrityError as exc:
        raise WalletServiceError(
            'This transaction reference has already been used.',
            code='duplicate_tx_ref',
            status=409,
        ) from exc

    if method == DepositMethod.BANK_TRANSFER:
        payment_method = {
            'type': 'bank_transfer',
            'bank_transfer': {
                'account_type': 'dynamic',
                'account_expires_in': 1800,  # 30 minutes
                'account_display_name': user.full_name or user.email,
            },
        }
    else:
        payment_method = {'type': 'ussd', 'ussd': {'account_bank': bank_code}}

    try:
        payload = flutterwave.create_direct_order(
            amount=float(amount),
            currency='NGN',
            reference=tx_ref,
            customer=_customer_payload(user),
            payment_method=payment_method,
        )
    except FlutterwaveError as exc:
        tx.status = TransactionStatus.FAILED
        tx.note = f'Order creation failed: {exc.message}'
        tx.save(update_fields=['status', 'note', 'updated_at'])
        raise WalletServiceError(
            'Could not start this deposit. Please try again.',
            code='flw_unreachable',
            status=502,
        ) from exc

    if payload.get('status') != 'success':
        tx.status = TransactionStatus.FAILED
        tx.note = payload.get('message', 'Order rejected by Flutterwave.')
        tx.save(update_fields=['status', 'note', 'updated_at'])
        raise WalletServiceError(
            'Could not start this deposit. Please try again.', code='order_rejected'
        )

    data = payload.get('data') or {}
    tx.flw_order_id = data.get('id', '')

    next_action = data.get('next_action') or {}
    if next_action.get('type') == 'requires_bank_transfer':
        details = next_action.get('requires_bank_transfer') or {}
        tx.account_number = details.get('account_number', '')
        tx.bank_name = details.get('account_bank_name', '')
        expires_raw = details.get('account_expiration_datetime')
        if expires_raw:
            parsed = datetime.fromisoformat(expires_raw.replace('Z', '+00:00'))
            tx.virtual_account_expires_at = parsed
        tx.note = details.get('note', 'Transfer this amount to complete payment.')
    else:
        instruction = next_action.get('payment_instruction') or {}
        tx.note = instruction.get('note', 'Follow the prompt on your phone to complete payment.')

    tx.save(
        update_fields=[
            'flw_order_id', 'account_number', 'bank_name',
            'virtual_account_expires_at', 'note', 'updated_at',
        ]
    )
    return tx


def _settle_deposit(tx: Transaction) -> Transaction:
    """Re-fetch the order from Flutterwave and apply completed/failed/pending."""
    if tx.status == TransactionStatus.COMPLETED:
        return tx
    if not tx.flw_order_id:
        return tx

    try:
        payload = flutterwave.get_order(tx.flw_order_id)
    except FlutterwaveError as exc:
        raise WalletServiceError(
            f'Could not check payment status: {exc.message}',
            code='flw_unreachable',
            status=502,
        ) from exc

    data = payload.get('data') or {}
    flw_status = data.get('status')
    paid_amount = Decimal(str(data.get('amount', 0)))

    if flw_status in _ORDER_COMPLETED_STATUSES and paid_amount >= tx.amount:
        with transaction.atomic():
            locked_tx = Transaction.objects.select_for_update().get(pk=tx.pk)
            if locked_tx.status == TransactionStatus.COMPLETED:
                return locked_tx
            wallet = Wallet.objects.select_for_update().get(user=locked_tx.user)
            wallet.ngn_balance = wallet.ngn_balance + locked_tx.amount
            wallet.save(update_fields=['ngn_balance', 'updated_at'])
            locked_tx.status = TransactionStatus.COMPLETED
            locked_tx.save(update_fields=['status', 'updated_at'])
            return locked_tx
    elif flw_status in _ORDER_FAILED_STATUSES:
        tx.status = TransactionStatus.FAILED
        tx.note = f'Order {flw_status} at Flutterwave.'
        tx.save(update_fields=['status', 'note', 'updated_at'])

    return tx


def verify_payment(user, tx_ref: str) -> tuple[Wallet, Transaction]:
    """Client-triggered "have I paid yet?" check — safe to call repeatedly."""
    try:
        tx = Transaction.objects.get(
            flw_tx_ref=tx_ref, user=user, tx_type=TransactionType.DEPOSIT
        )
    except Transaction.DoesNotExist as exc:
        raise WalletServiceError(
            'No deposit found for this reference.', code='tx_not_found', status=404
        ) from exc

    tx = _settle_deposit(tx)
    return get_or_create_wallet(user), tx


def handle_deposit_webhook(order_reference: str) -> None:
    """Order/charge webhook — re-verify via GET before crediting, never trust
    the webhook payload's status directly."""
    if not order_reference:
        return
    tx = Transaction.objects.filter(
        flw_tx_ref=order_reference, tx_type=TransactionType.DEPOSIT
    ).first()
    if tx is None or tx.status == TransactionStatus.COMPLETED:
        return
    _settle_deposit(tx)


# ─── Withdrawal ───────────────────────────────────────────────────────────


@transaction.atomic
def _debit_and_create_withdrawal(
    user, amount: Decimal, account_name: str, bank_name: str, bank_code: str,
    account_number: str,
) -> Transaction:
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    if wallet.ngn_balance < amount:
        raise WalletServiceError('Insufficient balance.', code='insufficient_balance')

    wallet.ngn_balance = wallet.ngn_balance - amount
    wallet.save(update_fields=['ngn_balance', 'updated_at'])

    for _ in range(5):
        reference = generate_withdrawal_reference()
        try:
            return Transaction.objects.create(
                user=user,
                tx_type=TransactionType.WITHDRAWAL,
                amount=amount,
                status=TransactionStatus.PENDING,
                reference=reference,
                account_name=account_name,
                bank_name=bank_name,
                bank_code=bank_code,
                account_number=account_number,
            )
        except IntegrityError:
            continue
    raise WalletServiceError('Could not generate a withdrawal reference.', status=500)


def _refund_and_fail(tx_id, note: str) -> None:
    with transaction.atomic():
        tx = Transaction.objects.select_for_update().get(pk=tx_id)
        if tx.status != TransactionStatus.PENDING:
            return
        wallet = Wallet.objects.select_for_update().get(user=tx.user)
        wallet.ngn_balance = wallet.ngn_balance + tx.amount
        wallet.save(update_fields=['ngn_balance', 'updated_at'])

        tx.status = TransactionStatus.FAILED
        tx.note = note
        tx.save(update_fields=['status', 'note', 'updated_at'])


def request_withdrawal(
    user,
    *,
    amount: Decimal,
    account_name: str,
    bank_name: str,
    bank_code: str,
    account_number: str,
) -> Transaction:
    if amount < MIN_WITHDRAWAL:
        raise WalletServiceError(
            f'Minimum withdrawal amount is ₦{MIN_WITHDRAWAL}.', code='amount_too_low'
        )
    if len(account_number) != 10 or not account_number.isdigit():
        raise WalletServiceError(
            'Account number must be exactly 10 digits.', code='invalid_account_number'
        )

    tx = _debit_and_create_withdrawal(
        user, amount, account_name, bank_name, bank_code, account_number
    )

    try:
        recipient_payload = flutterwave.create_recipient(
            account_number=account_number, bank_code=bank_code
        )
    except FlutterwaveError as exc:
        _refund_and_fail(tx.pk, f'Recipient creation failed: {exc.message}')
        raise WalletServiceError(
            'Withdrawal could not be initiated. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        ) from exc

    if recipient_payload.get('status') != 'success':
        note = recipient_payload.get('message', 'Recipient rejected by Flutterwave.')
        _refund_and_fail(tx.pk, note)
        raise WalletServiceError(
            'Withdrawal could not be initiated. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        )

    recipient_id = (recipient_payload.get('data') or {}).get('id')

    try:
        transfer_payload = flutterwave.create_transfer(
            recipient_id=recipient_id,
            amount=float(amount),
            reference=tx.reference,
            narration=f'[{getattr(settings, "APP_NAME", "CrownEx")}] Withdrawal',
        )
    except FlutterwaveError as exc:
        _refund_and_fail(tx.pk, f'Transfer request failed: {exc.message}')
        raise WalletServiceError(
            'Withdrawal could not be initiated. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        ) from exc

    if transfer_payload.get('status') != 'success':
        note = transfer_payload.get('message', 'Transfer rejected by Flutterwave.')
        _refund_and_fail(tx.pk, note)
        raise WalletServiceError(
            'Withdrawal could not be initiated. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        )

    data = transfer_payload.get('data') or {}
    tx.flw_transfer_id = data.get('id', '')
    tx.save(update_fields=['flw_transfer_id', 'updated_at'])

    # A transfer can fail synchronously on creation (e.g. invalid account
    # rejected immediately) rather than only via a later webhook.
    if data.get('status') in _TRANSFER_FAILED_STATUSES:
        _refund_and_fail(tx.pk, f'Transfer {data.get("status", "").lower()} by Flutterwave.')
        raise WalletServiceError(
            'Withdrawal could not be completed. Your balance has been refunded.',
            code='transfer_failed',
            status=502,
        )

    return tx


def _settle_transfer(tx: Transaction) -> None:
    """Re-fetch the transfer from Flutterwave and apply success/failure."""
    if tx.status != TransactionStatus.PENDING or not tx.flw_transfer_id:
        return

    try:
        payload = flutterwave.get_transfer(tx.flw_transfer_id)
    except FlutterwaveError:
        return  # Transient — a later webhook delivery or poll will retry.

    data = payload.get('data') or {}
    flw_status = data.get('status')

    if flw_status in _TRANSFER_SUCCESS_STATUSES:
        with transaction.atomic():
            locked = Transaction.objects.select_for_update().get(pk=tx.pk)
            if locked.status != TransactionStatus.PENDING:
                return
            locked.status = TransactionStatus.COMPLETED
            locked.save(update_fields=['status', 'updated_at'])
    elif flw_status in _TRANSFER_FAILED_STATUSES:
        _refund_and_fail(tx.pk, f'Transfer {flw_status.lower()} by Flutterwave.')


def handle_transfer_webhook(reference: str) -> None:
    if not reference:
        return
    tx = Transaction.objects.filter(
        reference=reference, tx_type=TransactionType.WITHDRAWAL
    ).first()
    if tx is None:
        return
    _settle_transfer(tx)


# ─── Banks / account resolution ────────────────────────────────────────────


def list_banks() -> list[dict]:
    try:
        payload = flutterwave.get_banks()
    except FlutterwaveError as exc:
        raise WalletServiceError(
            f'Could not load banks: {exc.message}', code='flw_unreachable', status=502
        ) from exc

    data = payload.get('data') or []
    return [{'name': b.get('name'), 'code': b.get('code')} for b in data]


def resolve_account_name(account_number: str, bank_code: str) -> str:
    try:
        payload = flutterwave.resolve_account(
            account_number=account_number, bank_code=bank_code
        )
    except FlutterwaveError as exc:
        raise WalletServiceError(
            f'Could not resolve account: {exc.message}', code='flw_unreachable', status=502
        ) from exc

    if payload.get('status') != 'success':
        raise WalletServiceError(
            payload.get('message', 'Could not resolve this account.'),
            code='resolve_failed',
        )

    data = payload.get('data') or {}
    account_name = data.get('account_name')
    if not account_name:
        raise WalletServiceError('Could not resolve this account.', code='resolve_failed')
    return account_name


def list_transactions(user):
    return Transaction.objects.filter(user=user).order_by('-created_at')[:50]
