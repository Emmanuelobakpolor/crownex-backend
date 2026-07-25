"""Business logic for crypto trading.

Built up in phases:
  Phase 1 — live price resolution against Quidax tickers.
  Phase 2 — admin-controlled platform fees, computed fresh at quote time
            and then frozen onto the quote (never recomputed at order time —
            see the CryptoQuote model in a later phase for why).
Quotes, wallets, and order execution land in subsequent phases.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import quidax
from .models import CryptoFeeSettings, CryptoQuote, CryptoWallet, FeeType, QuoteType
from .quidax import QuidaxError

_TICKERS_CACHE_KEY = 'crypto:quidax:tickers'
_TICKERS_CACHE_TTL = 15  # seconds — short enough to keep quotes fresh, long
# enough to absorb bursts of quote requests without hammering Quidax.

MIN_ORDER_NOTIONAL_NGN = Decimal('1000')


class CryptoServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def check_transaction_pin(user, pin: str) -> None:
    """Same guard as vtu.services / giftcards.services — every crypto action
    that moves money requires the user's 4-digit transaction PIN."""
    if not user.has_transaction_pin:
        raise CryptoServiceError(
            'Set a transaction PIN before trading.', code='pin_not_set'
        )
    if not user.check_transaction_pin(pin):
        raise CryptoServiceError('Incorrect transaction PIN.', code='invalid_pin', status=401)


# Server-side coin catalogue — the client never hardcodes markets. Each
# entry maps our symbol to how its NGN price is resolved: a direct NGN
# market where Quidax has one, otherwise via its USDT market * usdtngn.
SUPPORTED_COINS: dict[str, dict] = {
    'btc': {'name': 'Bitcoin', 'market': 'btcngn'},
    'eth': {'name': 'Ethereum', 'market': 'ethngn'},
    'usdt': {'name': 'Tether', 'market': 'usdtngn'},
    'sol': {'name': 'Solana', 'market': 'solusdt', 'via_usdt': True},
}


# ─── Prices ─────────────────────────────────────────────────────────────────


def _raw_tickers() -> dict:
    cached = cache.get(_TICKERS_CACHE_KEY)
    if cached is not None:
        return cached

    try:
        payload = quidax.get_all_tickers()
    except QuidaxError as exc:
        raise CryptoServiceError(
            f'Could not load live prices: {exc.message}',
            code='quidax_unreachable',
            status=502,
        )

    tickers = (payload.get('data') or {}) if isinstance(payload, dict) else {}
    if tickers:
        cache.set(_TICKERS_CACHE_KEY, tickers, _TICKERS_CACHE_TTL)
    return tickers


def _market_last_price(tickers: dict, market: str) -> Decimal | None:
    entry = tickers.get(market)
    if not entry:
        return None
    ticker = entry.get('ticker') or {}
    last = ticker.get('last')
    if last is None:
        return None
    try:
        price = Decimal(str(last))
    except Exception:
        return None
    return price if price > 0 else None


def _resolve_rate(tickers: dict, coin: str) -> Decimal | None:
    meta = SUPPORTED_COINS[coin]
    if meta.get('via_usdt'):
        usdt_price = _market_last_price(tickers, meta['market'])
        usdt_ngn = _market_last_price(tickers, 'usdtngn')
        if usdt_price is None or usdt_ngn is None:
            return None
        return usdt_price * usdt_ngn
    return _market_last_price(tickers, meta['market'])


def get_coin_rate_ngn(coin: str) -> Decimal:
    """Resolve a single coin's live NGN rate — direct NGN market first, else via USDT."""
    coin = coin.lower()
    if coin not in SUPPORTED_COINS:
        raise CryptoServiceError('Unsupported coin.', code='unsupported_coin')

    rate = _resolve_rate(_raw_tickers(), coin)
    if rate is None:
        raise CryptoServiceError(
            f'Live price unavailable for {coin.upper()}.', code='price_unavailable', status=502
        )
    return rate


def get_prices() -> list[dict]:
    """Public price list for every supported coin, for client display."""
    tickers = _raw_tickers()
    rows = []
    for symbol, meta in SUPPORTED_COINS.items():
        rate = _resolve_rate(tickers, symbol)
        rows.append(
            {
                'symbol': symbol,
                'name': meta['name'],
                'rate_ngn': str(rate) if rate is not None else None,
            }
        )
    return rows


# ─── Fees ───────────────────────────────────────────────────────────────────

_DEFAULT_FEE_TYPES = [choice.value for choice in FeeType]


def get_all_fee_settings() -> list[CryptoFeeSettings]:
    """Every fee row, auto-creating buy/sell/swap/withdraw at 0/0 if missing
    so the admin fee page always has all four cards to show."""
    existing = {row.fee_type: row for row in CryptoFeeSettings.objects.all()}
    missing = [ft for ft in _DEFAULT_FEE_TYPES if ft not in existing]
    if missing:
        CryptoFeeSettings.objects.bulk_create(
            [CryptoFeeSettings(fee_type=ft) for ft in missing], ignore_conflicts=True
        )
        existing = {row.fee_type: row for row in CryptoFeeSettings.objects.all()}
    return [existing[ft] for ft in _DEFAULT_FEE_TYPES]


def get_fee_settings(fee_type: str) -> CryptoFeeSettings:
    settings_row, _ = CryptoFeeSettings.objects.get_or_create(fee_type=fee_type)
    return settings_row


def get_public_fees() -> dict:
    """{ fees: { buy: {flat_usd, percent}, sell: {...}, ... } } — estimate
    display only; the authoritative fee is whatever's frozen onto a quote."""
    return {
        row.fee_type: {'flat_usd': str(row.flat_usd), 'percent': str(row.percent)}
        for row in get_all_fee_settings()
    }


def compute_fee_ngn(fee_type: str, ngn_value: Decimal) -> Decimal:
    """flat_ngn + pct_ngn, using NGN_PER_USD to convert the flat USD leg.
    Called fresh at quote time only — never re-read at order execution."""
    row = get_fee_settings(fee_type)
    ngn_per_usd = Decimal(str(settings.NGN_PER_USD))
    flat_ngn = row.flat_usd * ngn_per_usd
    pct_ngn = ngn_value * row.percent / Decimal('100')
    return (flat_ngn + pct_ngn).quantize(Decimal('0.01'))


# ─── Wallets ────────────────────────────────────────────────────────────────


def get_or_create_wallet(user, coin: str) -> CryptoWallet:
    wallet, _ = CryptoWallet.objects.get_or_create(user=user, coin=coin.lower())
    return wallet


@transaction.atomic
def credit_crypto_available(user, coin: str, amount: Decimal) -> CryptoWallet:
    """Credit spendable balance — completed buys, swap-in leg, deposits (phase 7)."""
    wallet = CryptoWallet.objects.select_for_update().get_or_create(user=user, coin=coin)[0]
    wallet.available = wallet.available + amount
    wallet.save(update_fields=['available', 'updated_at'])
    return wallet


@transaction.atomic
def reserve_crypto(user, coin: str, amount: Decimal) -> CryptoWallet:
    """Move available -> reserved, locking it for an in-flight sell/swap/withdraw."""
    wallet = CryptoWallet.objects.select_for_update().get_or_create(user=user, coin=coin)[0]
    if wallet.available < amount:
        raise CryptoServiceError('Insufficient crypto balance.', code='insufficient_balance')
    wallet.available = wallet.available - amount
    wallet.reserved = wallet.reserved + amount
    wallet.save(update_fields=['available', 'reserved', 'updated_at'])
    return wallet


@transaction.atomic
def release_reserved_crypto(user, coin: str, amount: Decimal) -> CryptoWallet:
    """Move reserved -> available — the in-flight operation failed, give it back."""
    wallet = CryptoWallet.objects.select_for_update().get_or_create(user=user, coin=coin)[0]
    wallet.reserved = max(wallet.reserved - amount, Decimal('0'))
    wallet.available = wallet.available + amount
    wallet.save(update_fields=['available', 'reserved', 'updated_at'])
    return wallet


@transaction.atomic
def debit_reserved_crypto(user, coin: str, amount: Decimal) -> CryptoWallet:
    """Commit reserved as spent — the in-flight sell/swap/withdraw completed."""
    wallet = CryptoWallet.objects.select_for_update().get_or_create(user=user, coin=coin)[0]
    wallet.reserved = max(wallet.reserved - amount, Decimal('0'))
    wallet.save(update_fields=['reserved', 'updated_at'])
    return wallet


def list_wallets(user) -> list[CryptoWallet]:
    """One row per supported coin — creates any missing ones at zero so the
    wallets screen always shows the full coin set, not just ones touched so far."""
    existing = {w.coin: w for w in CryptoWallet.objects.filter(user=user)}
    missing = [coin for coin in SUPPORTED_COINS if coin not in existing]
    if missing:
        CryptoWallet.objects.bulk_create(
            [CryptoWallet(user=user, coin=coin) for coin in missing], ignore_conflicts=True
        )
        existing = {w.coin: w for w in CryptoWallet.objects.filter(user=user)}
    return [existing[coin] for coin in SUPPORTED_COINS]


# ─── Quotes ─────────────────────────────────────────────────────────────────

QUOTE_TTL_SECONDS = 30


def _validate_coin(coin: str) -> str:
    coin = coin.lower()
    if coin not in SUPPORTED_COINS:
        raise CryptoServiceError('Unsupported coin.', code='unsupported_coin')
    return coin


@transaction.atomic
def create_quote(
    user, *, quote_type: str, coin: str, amount: Decimal, to_coin: str | None = None
) -> CryptoQuote:
    if quote_type not in (QuoteType.BUY, QuoteType.SELL, QuoteType.SWAP):
        raise CryptoServiceError('Invalid quote type.', code='invalid_type')
    if amount is None or amount <= 0:
        raise CryptoServiceError('Amount must be greater than zero.', code='invalid_amount')

    coin = _validate_coin(coin)
    rate = get_coin_rate_ngn(coin)
    ngn_value = (rate * amount).quantize(Decimal('0.01'))

    if ngn_value < MIN_ORDER_NOTIONAL_NGN:
        raise CryptoServiceError(
            f'Minimum order amount is ₦{MIN_ORDER_NOTIONAL_NGN}.', code='amount_too_low'
        )

    to_rate = None
    to_coin_amount = None
    to_coin_clean = ''

    if quote_type == QuoteType.SWAP:
        if not to_coin:
            raise CryptoServiceError('to_coin is required for a swap quote.', code='to_coin_required')
        to_coin_clean = _validate_coin(to_coin)
        if to_coin_clean == coin:
            raise CryptoServiceError('Cannot swap a coin into itself.', code='same_coin')
        to_rate = get_coin_rate_ngn(to_coin_clean)

        fee_ngn = compute_fee_ngn(FeeType.SWAP, ngn_value)
        net_ngn = ngn_value - fee_ngn
        if net_ngn <= 0:
            raise CryptoServiceError('Amount too small after fees.', code='amount_too_low')
        to_coin_amount = (net_ngn / to_rate).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        total_ngn = ngn_value  # notional; fee is deducted from the destination leg, not added on top

    elif quote_type == QuoteType.BUY:
        fee_ngn = compute_fee_ngn(FeeType.BUY, ngn_value)
        total_ngn = ngn_value + fee_ngn

    else:  # SELL
        fee_ngn = compute_fee_ngn(FeeType.SELL, ngn_value)
        total_ngn = max(ngn_value - fee_ngn, Decimal('0'))

    return CryptoQuote.objects.create(
        user=user,
        quote_type=quote_type,
        coin=coin,
        to_coin=to_coin_clean,
        coin_amount=amount,
        rate_ngn=rate,
        to_rate_ngn=to_rate,
        fee_ngn=fee_ngn,
        total_ngn=total_ngn,
        to_coin_amount=to_coin_amount,
    )


def get_locked_quote(user, quote_id, expected_type: str) -> CryptoQuote:
    """Fetch + validate a quote for order placement. Does NOT mark it used —
    callers must call mark_quote_used() inside the same DB transaction as
    the order create, per the 'copy from quote, mark used atomically' rule."""
    try:
        quote = CryptoQuote.objects.select_for_update().get(pk=quote_id, user=user)
    except (CryptoQuote.DoesNotExist, ValueError):
        raise CryptoServiceError('Quote not found.', code='quote_not_found', status=404)

    if quote.quote_type != expected_type:
        raise CryptoServiceError('This quote is not for this order type.', code='quote_type_mismatch')
    if quote.used_at is not None:
        raise CryptoServiceError('This quote has already been used.', code='quote_used', status=409)
    if quote.is_expired:
        raise CryptoServiceError(
            'This quote has expired. Please request a new one.', code='quote_expired', status=409
        )
    return quote


def mark_quote_used(quote: CryptoQuote) -> None:
    quote.used_at = timezone.now()
    quote.save(update_fields=['used_at'])
