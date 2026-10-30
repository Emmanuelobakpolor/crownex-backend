from django.contrib import admin

from .models import CryptoWithdrawMinimum


@admin.register(CryptoWithdrawMinimum)
class CryptoWithdrawMinimumAdmin(admin.ModelAdmin):
    list_display = ('coin', 'network', 'min_amount', 'refused_below', 'updated_at')
    list_editable = ('min_amount',)
    search_fields = ('coin', 'network')
    readonly_fields = ('updated_at',)

    def save_model(self, request, obj, form, change):
        obj.coin = obj.coin.lower()
        obj.network = obj.network.upper()
        super().save_model(request, obj, form, change)
