"""Data models for crypto trading.

Built up in phases (see crypto/services.py and crypto/orders.py for the
reasoning behind each one). CryptoWallet is the source of truth for a
user's crypto balance — Quidax's own wallet balances are never shown to
the user directly. Deposit addresses and withdrawals land in later phases.
"""

import secrets
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class FeeType(models.TextChoices):
    BUY = 'buy', 'Buy'
    SELL = 'sell', 'Sell'
    SWAP = 'swap', 'Swap'
    WITHDRAW = 'withdraw', 'Withdraw'


class CryptoFeeSettings(models.Model):
    """One row per fee_type — admin-editable, read fresh at every quote.

    fee_ngn = (flat_usd * NGN_PER_USD) + (ngn_value * percent / 100)
    See crypto/services.py:compute_fee for the exact application per type.
    """

    fee_type = models.CharField(max_length=16, choices=FeeType.choices, unique=True)
    flat_usd = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['fee_type']
        verbose_name = 'crypto fee setting'
        verbose_name_plural = 'crypto fee settings'

    def __str__(self):
        return f'{self.fee_type}: ${self.flat_usd} + {self.percent}%'


class CryptoWallet(models.Model):
    """Internal per-user, per-coin ledger — the ONLY balance the app shows.

    available = spendable now. reserved = locked while a sell/swap/withdraw
    that debits this coin is in flight; released back to available if that
    operation fails, or drained to zero (debited) if it completes.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='crypto_wallets'
    )
    coin = models.CharField(max_length=10)
    available = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    reserved = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'coin'], name='unique_crypto_wallet_per_coin')
        ]

    def __str__(self):
        return f'{self.user.email} — {self.coin.upper()}: {self.available} (+{self.reserved} reserved)'

    @property
    def total(self):
        return self.available + self.reserved


class QuoteType(models.TextChoices):
    BUY = 'buy', 'Buy'
    SELL = 'sell', 'Sell'
    SWAP = 'swap', 'Swap'


def _default_quote_expiry():
    return timezone.now() + timedelta(seconds=30)


class CryptoQuote(models.Model):
    """A 30-second, one-time-use price+fee lock. Orders always copy their
    rate/fee/amounts FROM the quote rather than recomputing at execution —
    see crypto/services.py for why (prevents bait-and-switch and races)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='crypto_quotes'
    )
    quote_type = models.CharField(max_length=8, choices=QuoteType.choices)
    coin = models.CharField(max_length=10)
    to_coin = models.CharField(max_length=10, blank=True)  # swap only

    coin_amount = models.DecimalField(max_digits=24, decimal_places=8)
    rate_ngn = models.DecimalField(max_digits=18, decimal_places=2)
    to_rate_ngn = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    fee_ngn = models.DecimalField(max_digits=18, decimal_places=2)
    total_ngn = models.DecimalField(max_digits=18, decimal_places=2)
    to_coin_amount = models.DecimalField(
        max_digits=24, decimal_places=8, null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_default_quote_expiry)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', '-created_at'])]

    def __str__(self):
        return f'{self.quote_type} {self.coin_amount} {self.coin.upper()} — {self.user.email}'

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and not self.is_expired


def generate_order_reference() -> str:
    """`CRY-<ms epoch>-<6 hex chars>` — sent to Flutterwave as tx_ref for buys."""
    return f'CRY-{int(time.time() * 1000)}-{secrets.token_hex(3)}'


class OrderType(models.TextChoices):
    BUY = 'buy', 'Buy'
    SELL = 'sell', 'Sell'
    SWAP = 'swap', 'Swap'


class OrderStatus(models.TextChoices):
    """Not every status applies to every order_type — see crypto/orders.py
    for the lifecycle each type actually walks through:
      BUY:  pending_payment -> payment_received -> processing -> completed | failed
      SELL: waiting_deposit | processing -> completed | failed
      SWAP: processing -> completed | failed
    """

    PENDING_PAYMENT = 'pending_payment', 'Pending payment'
    PAYMENT_RECEIVED = 'payment_received', 'Payment received'
    WAITING_DEPOSIT = 'waiting_deposit', 'Waiting deposit'
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class CryptoOrder(models.Model):
    """A buy, sell, or swap — always created from an already-locked
    CryptoQuote, whose rate/fee/amounts are copied here as an immutable
    snapshot (never recomputed at execution time)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=64, unique=True)
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='crypto_orders'
    )
    quote = models.ForeignKey(
        CryptoQuote, on_delete=models.CASCADE, related_name='orders'
    )

    order_type = models.CharField(max_length=8, choices=OrderType.choices)
    coin = models.CharField(max_length=10)
    to_coin = models.CharField(max_length=10, blank=True)

    coin_amount = models.DecimalField(max_digits=24, decimal_places=8)
    to_coin_amount = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    rate_ngn = models.DecimalField(max_digits=18, decimal_places=2)
    to_rate_ngn = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    fee_ngn = models.DecimalField(max_digits=18, decimal_places=2)
    total_ngn = models.DecimalField(max_digits=18, decimal_places=2)

    status = models.CharField(
        max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING_PAYMENT
    )

    quidax_order_id = models.CharField(max_length=64, blank=True)
    quidax_sell_order_id = models.CharField(max_length=64, blank=True)  # swap leg 1
    deposit_address = models.CharField(max_length=255, blank=True)  # sell, waiting_deposit
    payment_proof = models.ImageField(upload_to='crypto_proofs/', null=True, blank=True)
    flw_tx_ref = models.CharField(max_length=64, blank=True)

    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.order_type} {self.coin_amount} {self.coin.upper()} ({self.status}) — {self.user.email}'


class CryptoOrderLog(models.Model):
    """Immutable event stream per order — the audit trail admins read to
    understand exactly what happened without guessing from status alone."""

    order = models.ForeignKey(CryptoOrder, on_delete=models.CASCADE, related_name='logs')
    event = models.CharField(max_length=64)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.event} — order {self.order.reference}'


class QuidaxSubAccount(models.Model):
    """Links a CrownEx user to their Quidax sub-account.

    A field on User itself would work too, but every other cross-app
    relationship in this codebase (Wallet, VTUTransaction, GiftCardPurchase,
    CryptoWallet) is a FK to settings.AUTH_USER_MODEL rather than a field
    added onto User — this follows the same pattern instead of touching the
    shared accounts app. Provisioned lazily (see crypto/deposits.py) the
    first time a user actually needs a deposit address, not at
    registration, so a Quidax outage never blocks signup.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='quidax_subaccount'
    )
    quidax_user_id = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.email} -> quidax:{self.quidax_user_id}'


class CryptoDepositAddress(models.Model):
    """Cached per-user, per-coin (per-network) deposit address — fetched
    once from Quidax and reused forever after. Addresses are never shared
    between users."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='crypto_deposit_addresses'
    )
    coin = models.CharField(max_length=10)
    network = models.CharField(max_length=20, blank=True)  # e.g. TRC20/ERC20; blank for single-network coins
    address = models.CharField(max_length=255)
    quidax_ref = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'coin', 'network'], name='unique_deposit_address_per_coin_network'
            )
        ]

    def __str__(self):
        return f'{self.user.email} — {self.coin.upper()} ({self.network or "default"}): {self.address}'


class CryptoDepositEvent(models.Model):
    """One row per Quidax deposit already credited — the webhook's
    idempotency guard against provider retries (Quidax may resend the same
    deposit.successful event more than once)."""

    quidax_deposit_id = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='crypto_deposit_events'
    )
    coin = models.CharField(max_length=10)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'deposit {self.quidax_deposit_id}: {self.amount} {self.coin.upper()} -> {self.user.email}'


def generate_withdrawal_reference() -> str:
    """`CRYWD-<ms epoch>-<6 hex chars>` — sent to Quidax as the withdrawal reference."""
    return f'CRYWD-{int(time.time() * 1000)}-{secrets.token_hex(3)}'


class WithdrawalStatus(models.TextChoices):
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'
    REJECTED = 'rejected', 'Rejected'


class CryptoWithdrawal(models.Model):
    """An on-chain withdrawal. No admin approval step — strict guards at
    request time (address format, min notional, daily cap, balance
    reservation) are what keep this safe to run unattended.

    amount is gross — what's reserved and ultimately debited from the
    user's CryptoWallet. fee_coin is the platform's withdraw-fee cut
    (computed like the other fee types, just expressed in the withdrawn
    coin instead of NGN). net_amount = amount - fee_coin is what actually
    gets sent on-chain to the destination address.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=64, unique=True)
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='crypto_withdrawals'
    )

    coin = models.CharField(max_length=10)
    network = models.CharField(max_length=20, blank=True)
    address = models.CharField(max_length=255)

    amount = models.DecimalField(max_digits=24, decimal_places=8)
    fee_coin = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    net_amount = models.DecimalField(max_digits=24, decimal_places=8)
    rate_ngn = models.DecimalField(max_digits=18, decimal_places=2)  # snapshot, for the daily NGN cap

    status = models.CharField(
        max_length=16, choices=WithdrawalStatus.choices, default=WithdrawalStatus.PROCESSING
    )
    quidax_withdrawal_id = models.CharField(max_length=64, blank=True)
    tx_id = models.CharField(max_length=128, blank=True)
    note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'withdraw {self.amount} {self.coin.upper()} ({self.status}) — {self.user.email}'


class CryptoWithdrawalLog(models.Model):
    """Immutable event stream per withdrawal, same purpose as CryptoOrderLog."""

    withdrawal = models.ForeignKey(CryptoWithdrawal, on_delete=models.CASCADE, related_name='logs')
    event = models.CharField(max_length=64)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.event} — withdrawal {self.withdrawal.reference}'
