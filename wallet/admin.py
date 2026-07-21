from django.contrib import admin

from .models import Transaction, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'ngn_balance', 'updated_at')
    search_fields = ('user__email',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        'reference',
        'user',
        'tx_type',
        'amount',
        'status',
        'created_at',
    )
    list_filter = ('tx_type', 'status')
    search_fields = ('reference', 'flw_tx_ref', 'user__email', 'account_number')
    readonly_fields = ('id', 'created_at', 'updated_at')
