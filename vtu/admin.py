from django.contrib import admin

from .models import VTUTransaction


@admin.register(VTUTransaction)
class VTUTransactionAdmin(admin.ModelAdmin):
    list_display = ['reference', 'user', 'service', 'network', 'amount', 'status', 'created_at']
    list_filter = ['service', 'status', 'network']
    search_fields = ['reference', 'provider_ref', 'phone', 'user__email']
    readonly_fields = [f.name for f in VTUTransaction._meta.fields]
