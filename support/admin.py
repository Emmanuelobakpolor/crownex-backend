from django.contrib import admin

from .models import SupportConversation, SupportMessage


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    readonly_fields = ['sender_type', 'admin', 'body', 'created_at']


@admin.register(SupportConversation)
class SupportConversationAdmin(admin.ModelAdmin):
    list_display = ['user', 'status', 'last_message_at']
    list_filter = ['status']
    search_fields = ['user__email', 'user__full_name']
    inlines = [SupportMessageInline]
