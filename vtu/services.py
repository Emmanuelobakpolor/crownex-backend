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

_CATALOGUE_CACHE_KEY = 'vtu:pluginng:plans'
_CATALOGUE_CACHE_TTL = 300  # seconds


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
    """Cached GET /get/plans data — avoids hitting PluginNG on every purchase."""
    cached = cache.get(_CATALOGUE_CACHE_KEY)
    if cached is not None:
        return cached

    try:
        payload = pluginng.get_plans()
    except PluginNGError as exc:
        raise VTUServiceError(
            f'Could not load plans: {exc.message}', code='pluginng_unreachable', status=502
        )

    data = payload.get('data') or []
    if data:
        cache.set(_CATALOGUE_CACHE_KEY, data, _CATALOGUE_CACHE_TTL)
    return data


def get_catalogue() -> dict:
    """Airtime networks, plus data networks with their plan lists and prices."""
    airtime_networks = []
    data_networks = []
    for item in _raw_plans():
        if item.get('status') != '1':
            continue
        if item.get('category') == 'Airtime':
            airtime_networks.append(
                {'subcategory_id': item.get('subcategory_id'), 'name': item.get('title')}
            )
        elif item.get('category') == 'Data':
            data_networks.append(
                {
                    'subcategory_id': item.get('subcategory_id'),
                    'name': item.get('title'),
                    'plans': item.get('plan') or [],
                }
            )
    return {'airtime': airtime_networks, 'data': data_networks}


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
    *, user, service, network, subcategory_id, plan_id, phone, amount
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
