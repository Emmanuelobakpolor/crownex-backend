"""Read-only aggregation queries backing the admin dashboard.

Pulls from the models each domain app already owns (wallet, vtu, giftcards)
rather than duplicating any data — the admin panel has no models of its own.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from accounts.models import User
from giftcards.models import GiftCardPurchase, GiftCardStatus
from vtu.models import VTUStatus, VTUTransaction
from wallet.models import Transaction, TransactionStatus, TransactionType

# Cap per-source rows pulled into the in-memory merge/sort below — plenty for
# an admin feed (most recent activity), without scanning full tables as they
# grow.
MERGE_LIMIT_PER_SOURCE = 300


def _sum(queryset: QuerySet, field: str = 'amount') -> Decimal:
    return queryset.aggregate(total=Sum(field))['total'] or Decimal('0')


def _pct_change(current: int, prior: int) -> float | None:
    if prior == 0:
        return None
    return round(((current - prior) / prior) * 100, 1)


def get_overview_stats() -> dict:
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    completed_deposits = Transaction.objects.filter(
        tx_type=TransactionType.DEPOSIT, status=TransactionStatus.COMPLETED
    )
    completed_withdrawals = Transaction.objects.filter(
        tx_type=TransactionType.WITHDRAWAL, status=TransactionStatus.COMPLETED
    )

    new_users_this_week = User.objects.filter(date_joined__gte=week_ago).count()
    new_users_prior_week = User.objects.filter(
        date_joined__gte=two_weeks_ago, date_joined__lt=week_ago
    ).count()

    return {
        'total_users': User.objects.count(),
        'active_users': User.objects.filter(is_active=True).count(),
        'new_users_this_week': new_users_this_week,
        'new_users_growth_pct': _pct_change(new_users_this_week, new_users_prior_week),
        'deposit_today_ngn': str(_sum(completed_deposits.filter(created_at__gte=today_start))),
        'deposit_all_time_ngn': str(_sum(completed_deposits)),
        'deposit_count': Transaction.objects.filter(tx_type=TransactionType.DEPOSIT).count(),
        'withdrawal_today_ngn': str(_sum(completed_withdrawals.filter(created_at__gte=today_start))),
        'withdrawal_all_time_ngn': str(_sum(completed_withdrawals)),
        'withdrawal_count': Transaction.objects.filter(tx_type=TransactionType.WITHDRAWAL).count(),
        'vtu_total_ngn': str(_sum(VTUTransaction.objects.filter(status=VTUStatus.SUCCESS))),
        'vtu_count': VTUTransaction.objects.count(),
        'giftcard_total_ngn': str(
            _sum(
                GiftCardPurchase.objects.filter(status=GiftCardStatus.COMPLETED),
                field='amount_ngn',
            )
        ),
        'giftcard_count': GiftCardPurchase.objects.count(),
    }


def serialize_user(user: User) -> dict:
    try:
        balance = user.wallet.ngn_balance
    except Exception:
        balance = Decimal('0')

    return {
        'id': user.id,
        'name': user.full_name or user.email.split('@')[0],
        'email': user.email,
        'phone': user.phone,
        'balance_ngn': str(balance),
        'status': 'Active' if user.is_active else 'Inactive',
        'is_verified': user.is_verified,
        'last_active': (user.last_login or user.updated_at).isoformat(),
        'date_joined': user.date_joined.isoformat(),
    }


def get_users(search: str = '') -> QuerySet:
    qs = User.objects.select_related('wallet').all()
    if search:
        qs = qs.filter(Q(email__icontains=search) | Q(full_name__icontains=search))
    return qs


WALLET_TYPES = {'deposit', 'withdrawal'}
VTU_TYPES = {'airtime', 'data', 'cable', 'electricity'}


def get_unified_transactions(tx_type: str = 'all') -> list[dict]:
    """Merge wallet, VTU, and gift card activity into one feed, newest first."""

    tx_type = (tx_type or 'all').lower()
    items: list[dict] = []

    if tx_type in ('all', *WALLET_TYPES):
        qs = Transaction.objects.select_related('user').order_by('-created_at')
        if tx_type in WALLET_TYPES:
            qs = qs.filter(tx_type=tx_type)
        for tx in qs[:MERGE_LIMIT_PER_SOURCE]:
            items.append(
                {
                    'id': str(tx.id),
                    'type': tx.tx_type,
                    'user_email': tx.user.email,
                    'user_name': tx.user.full_name or tx.user.email,
                    'description': tx.get_tx_type_display(),
                    'amount': str(tx.amount),
                    'currency': 'NGN',
                    'status': tx.status,
                    'reference': tx.reference,
                    'created_at': tx.created_at,
                }
            )

    if tx_type in ('all', 'vtu', *VTU_TYPES):
        qs = VTUTransaction.objects.select_related('user').order_by('-created_at')
        if tx_type in VTU_TYPES:
            qs = qs.filter(service=tx_type)
        for tx in qs[:MERGE_LIMIT_PER_SOURCE]:
            items.append(
                {
                    'id': str(tx.id),
                    'type': tx.service,
                    'user_email': tx.user.email,
                    'user_name': tx.user.full_name or tx.user.email,
                    'description': f'{tx.get_service_display()} — {tx.network}'.strip(' —'),
                    'amount': str(tx.amount),
                    'currency': 'NGN',
                    'status': tx.status,
                    'reference': tx.reference,
                    'created_at': tx.created_at,
                }
            )

    if tx_type in ('all', 'giftcard'):
        qs = GiftCardPurchase.objects.select_related('user').order_by('-created_at')
        for tx in qs[:MERGE_LIMIT_PER_SOURCE]:
            items.append(
                {
                    'id': str(tx.id),
                    'type': 'giftcard',
                    'user_email': tx.user.email,
                    'user_name': tx.user.full_name or tx.user.email,
                    'description': tx.brand,
                    'amount': str(tx.amount_ngn),
                    'currency': 'NGN',
                    'status': tx.status,
                    'reference': tx.reference,
                    'created_at': tx.created_at,
                }
            )

    items.sort(key=lambda x: x['created_at'], reverse=True)
    return items
