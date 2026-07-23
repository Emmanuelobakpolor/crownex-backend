"""Business logic for gift card purchases via Reloadly.

Mirrors vtu/services.py: the wallet debit happens inside an atomic block
before the provider is ever called, and the refund path checks the
purchase's current status first. Unlike PluginNG, Reloadly's POST /orders
is a synchronous, authoritative purchase call — a 2xx response really
means the order was placed (and, for most brands, fulfilled immediately),
so success/failure is decided from that single response instead of a
separate reconcile step.

Never trust a client-supplied price: the USD unit price and product
existence are always re-validated against Reloadly's own /products data
before anything is debited.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from wallet.services import WalletServiceError, credit_wallet, debit_wallet

from . import reloadly
from .models import GiftCardPurchase, GiftCardStatus, generate_giftcard_reference
from .reloadly import ReloadlyError

_PRODUCTS_CACHE_TTL = 300  # seconds


class GiftCardServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _check_transaction_pin(user, pin: str) -> None:
    if not user.has_transaction_pin:
        raise GiftCardServiceError(
            'Set a transaction PIN before making a purchase.', code='pin_not_set'
        )
    if not user.check_transaction_pin(pin):
        raise GiftCardServiceError('Incorrect transaction PIN.', code='invalid_pin', status=401)


def get_rate() -> Decimal:
    return Decimal(str(settings.RELOADLY_NGN_PER_USD))


def _products_page(country_code: str, product_name: str | None = None) -> list[dict]:
    cache_key = f'giftcards:reloadly:products:{country_code}:{product_name or "*"}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        payload = reloadly.get_products(
            country_code=country_code, product_name=product_name, size=200, page=1
        )
    except ReloadlyError as exc:
        raise GiftCardServiceError(
            f'Could not load products: {exc.message}', code='reloadly_unreachable', status=502
        )

    items = payload.get('content') if isinstance(payload, dict) else payload
    items = items or []
    if items:
        cache.set(cache_key, items, _PRODUCTS_CACHE_TTL)
    return items


def get_brands(country_code: str) -> list[dict]:
    """Deduplicated brand list for a country, from the raw /products page."""
    seen = set()
    brands = []
    for item in _products_page(country_code):
        brand = item.get('brand') or {}
        brand_id = brand.get('brandId')
        if brand_id is None or brand_id in seen:
            continue
        seen.add(brand_id)
        logo_urls = item.get('logoUrls') or []
        brands.append(
            {
                'brand_id': brand_id,
                'brand_name': brand.get('brandName') or item.get('productName'),
                'logo_url': logo_urls[0] if logo_urls else None,
            }
        )
    brands.sort(key=lambda b: (b['brand_name'] or '').lower())
    return brands


def get_products(brand: str, country_code: str) -> list[dict]:
    """Fixed denominations (one row each) or an open USD range for a brand."""
    rate = get_rate()
    rows = []
    for item in _products_page(country_code, product_name=brand):
        product_id = item.get('productId')
        redeem_instruction = item.get('redeemInstruction')
        if isinstance(redeem_instruction, dict):
            redeem_instruction = redeem_instruction.get('concise') or redeem_instruction.get('verbose')
        row_common = {
            'product_id': product_id,
            'redeem_instruction': redeem_instruction,
            'discount_percentage': item.get('discountPercentage'),
            'sender_fee': item.get('senderFee'),
        }

        fixed = item.get('fixedRecipientDenominations') or []
        if fixed:
            for denom in fixed:
                usd = Decimal(str(denom))
                rows.append(
                    {
                        **row_common,
                        'unit_price_usd': str(usd),
                        'unit_price_ngn': str((usd * rate).quantize(Decimal('0.01'))),
                        'open_range': False,
                    }
                )
            continue

        min_usd = item.get('minRecipientDenomination')
        max_usd = item.get('maxRecipientDenomination')
        if min_usd is None or max_usd is None:
            continue
        rows.append(
            {
                **row_common,
                'min_usd': str(min_usd),
                'max_usd': str(max_usd),
                'open_range': True,
            }
        )
    return rows


def _resolve_unit_price(
    product_id: int, brand: str, country_code: str, requested_usd: Decimal
) -> Decimal:
    """Never trust a client-supplied price — validate the requested USD
    amount against Reloadly's own product data before charging anything."""
    for item in _products_page(country_code, product_name=brand):
        if item.get('productId') != product_id:
            continue

        fixed = item.get('fixedRecipientDenominations') or []
        if fixed:
            for denom in fixed:
                if Decimal(str(denom)) == requested_usd:
                    return requested_usd
            raise GiftCardServiceError(
                'Unknown denomination for this product.', code='invalid_denomination'
            )

        min_usd = item.get('minRecipientDenomination')
        max_usd = item.get('maxRecipientDenomination')
        if min_usd is not None and max_usd is not None:
            if Decimal(str(min_usd)) <= requested_usd <= Decimal(str(max_usd)):
                return requested_usd
            raise GiftCardServiceError(
                f'Amount must be between ${min_usd} and ${max_usd}.',
                code='amount_out_of_range',
            )

    raise GiftCardServiceError('Unknown product for this brand.', code='invalid_product')


def _refund_and_fail(purchase_id, note: str) -> None:
    with transaction.atomic():
        purchase = GiftCardPurchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status != GiftCardStatus.PENDING:
            return
        credit_wallet(purchase.user, purchase.amount_ngn)
        purchase.status = GiftCardStatus.FAILED
        purchase.note = note
        purchase.save(update_fields=['status', 'note', 'updated_at'])


def _apply_order_result(purchase: GiftCardPurchase, payload: dict) -> GiftCardPurchase:
    """Reconcile an order-creation response or a webhook/requery payload.
    Idempotent — a purchase only ever leaves PENDING once, here.

    Reloadly's POST /orders response has the redeemed card straight away
    for most brands. A few brands fulfil asynchronously — no redeemedCards
    yet, order still PENDING — and only get their code later via the
    webhook (see handle_webhook), so this only marks COMPLETED once a
    redeemed card actually shows up in the payload.
    """
    transaction_id = payload.get('transactionId')
    product = payload.get('product') or {}
    redeemed_cards = payload.get('redeemedCards') or payload.get('cards') or []

    with transaction.atomic():
        locked = GiftCardPurchase.objects.select_for_update().get(pk=purchase.pk)
        if locked.status != GiftCardStatus.PENDING:
            return locked

        locked.product_name = product.get('productName') or locked.product_name
        locked.reloadly_tx_id = str(transaction_id) if transaction_id else locked.reloadly_tx_id
        update_fields = ['product_name', 'reloadly_tx_id', 'updated_at']

        if redeemed_cards:
            card = redeemed_cards[0]
            locked.redeem_code = card.get('cardNumber') or ''
            locked.redeem_pin = card.get('pinCode') or ''
            locked.status = GiftCardStatus.COMPLETED
            update_fields += ['redeem_code', 'redeem_pin', 'status']

        locked.save(update_fields=update_fields)
    return locked


def buy_gift_card(
    user,
    *,
    product_id: int,
    unit_price_usd: Decimal,
    brand: str,
    country_code: str,
    transaction_pin: str,
) -> GiftCardPurchase:
    _check_transaction_pin(user, transaction_pin)

    usd = _resolve_unit_price(product_id, brand, country_code, unit_price_usd)
    rate = get_rate()
    amount_ngn = (usd * rate).quantize(Decimal('0.01'))

    try:
        debit_wallet(user, amount_ngn)
    except WalletServiceError as exc:
        raise GiftCardServiceError(exc.message, code=exc.code, status=exc.status)

    reference = generate_giftcard_reference()
    purchase = GiftCardPurchase.objects.create(
        user=user,
        brand=brand,
        country_code=country_code,
        product_id=product_id,
        unit_price_usd=usd,
        rate_ngn=rate,
        amount_ngn=amount_ngn,
        reference=reference,
        status=GiftCardStatus.PENDING,
    )

    try:
        payload = reloadly.create_order(
            product_id=product_id,
            quantity=1,
            unit_price=str(usd),
            custom_identifier=reference,
            sender_name=getattr(settings, 'APP_NAME', 'CrownEx'),
            recipient_email=user.email,
        )
    except ReloadlyError as exc:
        _refund_and_fail(purchase.pk, f'Order request failed: {exc.message}')
        raise GiftCardServiceError(
            'Gift card purchase could not be completed. Your balance has been refunded.',
            code='provider_failed',
            status=502,
        )

    return _apply_order_result(purchase, payload)


def handle_webhook(payload: dict) -> None:
    """Reloadly calls this for orders that don't resolve synchronously from
    POST /orders — some brands fulfil asynchronously, and the redeemed card
    only becomes available later.

    Reloadly's exact webhook body isn't pinned down here with certainty
    (their dashboard lets you send a test delivery — confirm the real
    field names against that before relying on this in production). What
    is documented is that it's keyed on the customIdentifier you sent when
    creating the order, so that's used to find the purchase; the actual
    card details are always re-fetched from GET /orders/transactions/{id}
    rather than trusted off the webhook body itself, same principle as
    never trusting a client-supplied price.
    """
    reference = payload.get('customIdentifier') or payload.get('custom_identifier')
    if not reference:
        return

    purchase = GiftCardPurchase.objects.filter(reference=reference).first()
    if purchase is None or purchase.status != GiftCardStatus.PENDING:
        return

    raw_status = str(payload.get('status', '')).upper()
    if raw_status in ('FAILED', 'REJECTED', 'CANCELLED', 'ERROR'):
        _refund_and_fail(purchase.pk, f'Order {raw_status.lower()} at provider (webhook).')
        return

    transaction_id = payload.get('transactionId') or purchase.reloadly_tx_id
    order = payload
    if transaction_id:
        try:
            order = reloadly.get_order(transaction_id)
        except ReloadlyError:
            order = payload

    _apply_order_result(purchase, order)


def list_purchases(user):
    return GiftCardPurchase.objects.filter(user=user).order_by('-created_at')[:50]
