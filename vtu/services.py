"""Business logic for VTU purchases (airtime, data) via PluginNG.

Mirrors wallet/services.py: every debit happens through wallet.services
inside an atomic block, and every refund path checks the transaction's
current status first — so a retried request, a manual status check, and
the PluginNG webhook can never double-charge or double-refund the same
purchase. PluginNG's purchase endpoints don't document a response schema,
so we never assume success from the initial call: a purchase starts
PENDING and is only finalized by requery or the webhook, exactly like the
wallet withdrawal flow.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction

from wallet.services import WalletServiceError, credit_wallet, debit_wallet

from . import pluginng
from .models import (
    PLUGINNG_STATUS_MAP,
    ServiceType,
    VTUStatus,
    VTUTransaction,
    generate_vtu_reference,
)
from .pluginng import PluginNGError

MIN_AIRTIME = Decimal('50')
MAX_AIRTIME = Decimal('50000')
MIN_ELECTRICITY = Decimal('500')
MAX_ELECTRICITY = Decimal('100000')

_CATALOGUE_CACHE_KEY = 'vtu:pluginng:plans'
_CATALOGUE_CACHE_TTL = 300  # seconds
_BOUQUET_CACHE_TTL = 600  # seconds — cable bouquets / electricity variation types


class VTUServiceError(Exception):
    """Domain error with a machine-readable code and HTTP status."""

    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if len(phone) != 11 or not phone.isdigit():
        raise VTUServiceError('Enter a valid 11-digit phone number.', code='invalid_phone')
    return phone


def _check_transaction_pin(user, pin: str) -> None:
    if not user.has_transaction_pin:
        raise VTUServiceError(
            'Set a transaction PIN before making a purchase.', code='pin_not_set'
        )
    if not user.check_transaction_pin(pin):
        raise VTUServiceError('Incorrect transaction PIN.', code='invalid_pin', status=401)


def _raw_plans() -> list[dict]:
    cached = cache.get(_CATALOGUE_CACHE_KEY)
    if cached is not None:
        return cached

    try:
        payload = pluginng.get_plans()
    except PluginNGError as exc:
        raise VTUServiceError(
            f'Could not load plans: {exc.message}',
            code='pluginng_unreachable',
            status=502,
        )

    data = payload.get('data') or []
    if data:
        cache.set(_CATALOGUE_CACHE_KEY, data, _CATALOGUE_CACHE_TTL)
    return data


def get_catalogue() -> dict:
    """Airtime/data networks, plus cable and electricity billers."""
    airtime_networks = []
    data_networks = []
    cable_billers = []
    electricity_billers = []
    for item in _raw_plans():
        # PluginNG returns status as an int (1), not the string shown in
        # their own docs example ("1") — compare on the stringified form.
        if str(item.get('status')) != '1':
            continue
        category = item.get('category')
        if category == 'Airtime':
            airtime_networks.append(
                {'subcategory_id': item.get('subcategory_id'), 'name': item.get('title')}
            )
        elif category == 'Data':
            data_networks.append(
                {
                    'subcategory_id': item.get('subcategory_id'),
                    'name': item.get('title'),
                    'plans': item.get('plan') or [],
                }
            )
        elif category == 'Cable':
            cable_billers.append(
                {'subcategory_id': item.get('subcategory_id'), 'name': item.get('title')}
            )
        elif category == 'Electricity':
            electricity_billers.append(
                {
                    'subcategory_id': item.get('subcategory_id'),
                    'name': item.get('title'),
                    'service_id': item.get('serviceID'),
                }
            )
    return {
        'airtime': airtime_networks,
        'data': data_networks,
        'cable': cable_billers,
        'electricity': electricity_billers,
    }


def _fetch_bouquet_data(network: str) -> list[dict]:
    """Cached GET /fetch/bouquet — cable bouquets or electricity variation types."""
    cache_key = f'vtu:pluginng:bouquet:{network}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        payload = pluginng.fetch_bouquet(network)
    except PluginNGError as exc:
        raise VTUServiceError(
            f'Could not load plans for {network}: {exc.message}',
            code='pluginng_unreachable',
            status=502,
        )

    data = payload.get('data') or []
    if data:
        cache.set(cache_key, data, _BOUQUET_CACHE_TTL)
    return data


def _cable_code(network: str) -> str:
    """PluginNG's cable endpoints (bouquet fetch, verify, purchase) all expect
    the lowercase network code ('gotv'/'dstv'/'startimes'), not the display
    title ('GOTV'/'DSTV'/'STARTIMES') that get/plans returns — electricity
    billers keep their exact catalogue title instead, so this is cable-only."""
    return network.lower()


def get_cable_bouquets(network: str) -> list[dict]:
    """Bouquets (variation_code/name/amount) for a cable biller like 'GOTV'."""
    return [
        {
            'variation_code': item.get('variation_code'),
            'name': item.get('name'),
            'amount': item.get('variation_amount'),
        }
        for item in _fetch_bouquet_data(_cable_code(network))
    ]


def get_electricity_variations(network: str) -> list[dict]:
    """Variation types for an electricity biller.

    PluginNG's docs for /purchase/electricity and /verify/card both fix
    variation_code/type to exactly "Prepaid" or "Postpaid" — there's no
    per-biller variation, and their /fetch/bouquet endpoint (which works
    fine for cable) reliably 500s for electricity billers, so we return
    these two directly instead of depending on it.
    """
    return [
        {'variation_code': 'Prepaid', 'name': 'Prepaid'},
        {'variation_code': 'Postpaid', 'name': 'Postpaid'},
    ]


def _resolve_cable_bouquet_amount(network: str, variation_code: str) -> Decimal:
    """Never trust a client-supplied price — look it up from PluginNG's bouquet list."""
    for item in _fetch_bouquet_data(_cable_code(network)):
        if item.get('variation_code') == variation_code:
            return Decimal(str(item.get('variation_amount', 0)))
    raise VTUServiceError('Unknown bouquet for this provider.', code='invalid_plan')


def verify_smartcard(network: str, cardno: str) -> dict:
    """POST /verify/card for cable — response shape isn't documented by
    PluginNG, so we pass through whatever they return and let the caller
    look for a recognizable name field rather than assuming one."""
    try:
        payload = pluginng.verify_card(plan=_cable_code(network), cardno=cardno)
    except PluginNGError as exc:
        raise VTUServiceError(
            f'Could not verify smartcard: {exc.message}', code='pluginng_unreachable', status=502
        )
    return payload.get('data') if isinstance(payload.get('data'), dict) else payload


def verify_meter(network: str, cardno: str, meter_type: str) -> dict:
    """POST /verify/card for electricity — same undocumented-shape caveat as
    verify_smartcard."""
    try:
        payload = pluginng.verify_card(plan=network, cardno=cardno, meter_type=meter_type)
    except PluginNGError as exc:
        raise VTUServiceError(
            f'Could not verify meter: {exc.message}', code='pluginng_unreachable', status=502
        )
    return payload.get('data') if isinstance(payload.get('data'), dict) else payload


def _resolve_data_plan_amount(subcategory_id: str, plan_id: str) -> Decimal:
    """Never trust a client-supplied price — look it up from PluginNG's catalogue."""
    for item in _raw_plans():
        if str(item.get('subcategory_id')) != str(subcategory_id):
            continue
        for plan in item.get('plan') or []:
            if plan.get('plan') == plan_id:
                return Decimal(str(plan.get('amount')))
    raise VTUServiceError('Unknown data plan for this network.', code='invalid_plan')


def _create_pending_tx(
    *, user, service, network, subcategory_id, plan_id, phone, amount, card_number=''
) -> VTUTransaction:
    for _ in range(5):
        reference = generate_vtu_reference()
        try:
            return VTUTransaction.objects.create(
                user=user,
                service=service,
                network=network,
                subcategory_id=subcategory_id,
                plan_id=plan_id,
                phone=phone,
                amount=amount,
                card_number=card_number,
                status=VTUStatus.PENDING,
                reference=reference,
            )
        except IntegrityError:
            continue
    raise VTUServiceError('Could not generate a transaction reference.', status=500)


def _refund_and_fail(tx_id, note: str) -> None:
    with transaction.atomic():
        tx = VTUTransaction.objects.select_for_update().get(pk=tx_id)
        if tx.status != VTUStatus.PENDING:
            return
        credit_wallet(tx.user, tx.amount)
        tx.status = VTUStatus.FAILED
        tx.note = note
        tx.save(update_fields=['status', 'note', 'updated_at'])


def _apply_provider_result(tx: VTUTransaction, payload: dict) -> VTUTransaction:
    """Reconcile a PluginNG purchase/requery/webhook payload. Idempotent."""
    data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    raw_status = str(data.get('status', '0'))
    provider_status = PLUGINNG_STATUS_MAP.get(raw_status, VTUStatus.PENDING)

    with transaction.atomic():
        locked = VTUTransaction.objects.select_for_update().get(pk=tx.pk)
        if locked.status != VTUStatus.PENDING:
            return locked

        locked.provider_ref = data.get('ref') or locked.provider_ref
        locked.provider_response = data.get('response') or locked.provider_response

        if provider_status == VTUStatus.SUCCESS:
            locked.status = VTUStatus.SUCCESS
            locked.save(update_fields=['status', 'provider_ref', 'provider_response', 'updated_at'])
        elif provider_status in (VTUStatus.FAILED, VTUStatus.REVERSED):
            locked.save(update_fields=['provider_ref', 'provider_response', 'updated_at'])
            _refund_and_fail(locked.pk, data.get('response') or 'Purchase failed at provider.')
        else:
            locked.save(update_fields=['provider_ref', 'provider_response', 'updated_at'])

    tx.refresh_from_db()
    return tx


def buy_airtime(
    user, *, subcategory_id: str, network: str, phone: str, amount: Decimal, transaction_pin: str
) -> VTUTransaction:
    _check_transaction_pin(user, transaction_pin)
    if amount < MIN_AIRTIME or amount > MAX_AIRTIME:
        raise VTUServiceError(
            f'Airtime amount must be between ₦{MIN_AIRTIME} and ₦{MAX_AIRTIME}.',
            code='amount_out_of_range',
        )
    phone = _normalize_phone(phone)

    try:
        debit_wallet(user, amount)
    except WalletServiceError as exc:
        raise VTUServiceError(exc.message, code=exc.code, status=exc.status)

    tx = _create_pending_tx(
        user=user, service=ServiceType.AIRTIME, network=network,
        subcategory_id=subcategory_id, plan_id='', phone=phone, amount=amount,
    )

    try:
        payload = pluginng.buy_airtime(
            amount=str(amount), phonenumber=phone,
            subcategory_id=subcategory_id, custom_reference=tx.reference,
        )
    except PluginNGError as exc:
        _refund_and_fail(tx.pk, f'Purchase request failed: {exc.message}')
        raise VTUServiceError(
            'Airtime purchase could not be completed. Your balance has been refunded.',
            code='provider_failed', status=502,
        )

    return _apply_provider_result(tx, payload)


def buy_data(
    user, *, subcategory_id: str, network: str, plan_id: str, phone: str, transaction_pin: str
) -> VTUTransaction:
    _check_transaction_pin(user, transaction_pin)
    phone = _normalize_phone(phone)
    amount = _resolve_data_plan_amount(subcategory_id, plan_id)

    try:
        debit_wallet(user, amount)
    except WalletServiceError as exc:
        raise VTUServiceError(exc.message, code=exc.code, status=exc.status)

    tx = _create_pending_tx(
        user=user, service=ServiceType.DATA, network=network,
        subcategory_id=subcategory_id, plan_id=plan_id, phone=phone, amount=amount,
    )

    try:
        payload = pluginng.buy_data(
            plan_id=plan_id, phonenumber=phone,
            subcategory_id=subcategory_id, custom_reference=tx.reference,
        )
    except PluginNGError as exc:
        _refund_and_fail(tx.pk, f'Purchase request failed: {exc.message}')
        raise VTUServiceError(
            'Data purchase could not be completed. Your balance has been refunded.',
            code='provider_failed', status=502,
        )

    return _apply_provider_result(tx, payload)


def buy_cable(
    user,
    *,
    subcategory_id: str,
    network: str,
    phone: str,
    cardno: str,
    variation_code: str,
    transaction_pin: str,
) -> VTUTransaction:
    _check_transaction_pin(user, transaction_pin)
    phone = _normalize_phone(phone)
    cardno = cardno.strip()
    if not cardno.isdigit():
        raise VTUServiceError('Enter a valid smartcard number.', code='invalid_cardno')
    amount = _resolve_cable_bouquet_amount(network, variation_code)

    try:
        debit_wallet(user, amount)
    except WalletServiceError as exc:
        raise VTUServiceError(exc.message, code=exc.code, status=exc.status)

    tx = _create_pending_tx(
        user=user, service=ServiceType.CABLE, network=network,
        subcategory_id=subcategory_id, plan_id=variation_code, phone=phone,
        amount=amount, card_number=cardno,
    )

    try:
        payload = pluginng.buy_cable(
            plan=_cable_code(network), phonenumber=phone, amount=str(amount),
            cardno=cardno, variation_code=variation_code,
            custom_reference=tx.reference,
        )
    except PluginNGError as exc:
        _refund_and_fail(tx.pk, f'Purchase request failed: {exc.message}')
        raise VTUServiceError(
            'Cable subscription could not be completed. Your balance has been refunded.',
            code='provider_failed', status=502,
        )

    return _apply_provider_result(tx, payload)


def buy_electricity(
    user,
    *,
    subcategory_id: str,
    network: str,
    service_id: str,
    phone: str,
    cardno: str,
    variation_code: str,
    amount: Decimal,
    transaction_pin: str,
) -> VTUTransaction:
    _check_transaction_pin(user, transaction_pin)
    phone = _normalize_phone(phone)
    cardno = cardno.strip()
    if not cardno.isdigit():
        raise VTUServiceError('Enter a valid meter number.', code='invalid_cardno')
    if amount < MIN_ELECTRICITY or amount > MAX_ELECTRICITY:
        raise VTUServiceError(
            f'Amount must be between ₦{MIN_ELECTRICITY} and ₦{MAX_ELECTRICITY}.',
            code='amount_out_of_range',
        )

    try:
        debit_wallet(user, amount)
    except WalletServiceError as exc:
        raise VTUServiceError(exc.message, code=exc.code, status=exc.status)

    tx = _create_pending_tx(
        user=user, service=ServiceType.ELECTRICITY, network=network,
        subcategory_id=subcategory_id, plan_id=variation_code, phone=phone,
        amount=amount, card_number=cardno,
    )

    try:
        payload = pluginng.buy_electricity(
            plan=network, phonenumber=phone, amount=str(amount), cardno=cardno,
            variation_code=variation_code, service_id=service_id,
            custom_reference=tx.reference,
        )
    except PluginNGError as exc:
        _refund_and_fail(tx.pk, f'Purchase request failed: {exc.message}')
        raise VTUServiceError(
            'Electricity payment could not be completed. Your balance has been refunded.',
            code='provider_failed', status=502,
        )

    return _apply_provider_result(tx, payload)


def requery_transaction(user, reference: str) -> VTUTransaction:
    try:
        tx = VTUTransaction.objects.get(reference=reference, user=user)
    except VTUTransaction.DoesNotExist:
        raise VTUServiceError('Transaction not found.', code='tx_not_found', status=404)

    if tx.status != VTUStatus.PENDING:
        return tx

    try:
        payload = pluginng.requery(reference)
    except PluginNGError as exc:
        raise VTUServiceError(
            f'Could not check status: {exc.message}', code='pluginng_unreachable', status=502
        )

    return _apply_provider_result(tx, payload)


def handle_webhook(payload: dict) -> None:
    custom_reference = payload.get('custom_reference')
    if not custom_reference:
        return
    tx = VTUTransaction.objects.filter(reference=custom_reference).first()
    if tx is None or tx.status != VTUStatus.PENDING:
        return
    _apply_provider_result(tx, payload)


def list_transactions(user):
    return VTUTransaction.objects.filter(user=user).order_by('-created_at')[:50]
