from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.notifications.models import TenantNotificationSettings


@admin.register(TenantNotificationSettings)
class TenantNotificationSettingsAdmin(ModelAdmin):
    """Администрирование настроек уведомлений тенантов."""

    list_display = ['tenant', 'telegram_chat_id', 'notify_email', 'notify_on_error', 'notify_on_critical']
    list_filter = ['notify_on_error', 'notify_on_critical']
    search_fields = ['tenant__slug', 'notify_email']
