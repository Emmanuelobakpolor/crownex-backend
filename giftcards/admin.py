from django.contrib import admin

from .models import GiftCardPurchase


@admin.register(GiftCardPurchase)
class GiftCardPurchaseAdmin(admin.ModelAdmin):
    list_display = ['reference', 'user', 'brand', 'unit_price_usd', 'amount_ngn', 'status', 'created_at']
    list_filter = ['status', 'brand', 'country_code']
    search_fields = ['reference', 'reloadly_tx_id', 'user__email']
    readonly_fields = [f.name for f in GiftCardPurchase._meta.fields]
