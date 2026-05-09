from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.sync.models import SyncLog


@admin.register(SyncLog)
class SyncLogAdmin(ModelAdmin):
    """Администрирование журнала синхронизации. Только чтение."""

    list_display = ['event_type', 'status', 'tenant', 'message_preview', 'created_at']
    list_filter = ['tenant', 'event_type', 'status']
    search_fields = ['message', 'tenant__slug']
    readonly_fields = ['tenant', 'product', 'listing', 'event_type', 'status', 'message', 'payload', 'created_at']

    def has_add_permission(self, request):
        """SyncLog создаётся только системой, добавление через Admin запрещено."""
        return False

    def has_change_permission(self, request, obj=None):
        """SyncLog не редактируется."""
        return False

    @admin.display(description='Сообщение')
    def message_preview(self, obj):
        """Первые 80 символов сообщения."""
        return obj.message[:80] + '...' if len(obj.message) > 80 else obj.message
