from django.contrib import admin

from apps.core.admin import TenantScopedAdminMixin, TenantScopedReadOnlyAdminMixin
from apps.notifications.models import NotificationDelivery, TenantNotificationSettings


@admin.register(TenantNotificationSettings)
class TenantNotificationSettingsAdmin(TenantScopedAdminMixin):
    """Администрирование настроек уведомлений тенантов."""

    list_display = [
        'tenant', 'telegram_chat_id', 'telegram_username',
        'notify_email', 'notify_on_error', 'notify_on_critical',
    ]
    list_filter = ['notify_on_error', 'notify_on_critical']
    search_fields = ['tenant__slug', 'notify_email', 'telegram_username']
    readonly_fields = ['connect_token', 'connect_token_expires_at']
    fieldsets = [
        ('Telegram', {
            'fields': ['tenant', 'telegram_chat_id', 'telegram_username'],
        }),
        ('Email и уровни уведомлений', {
            'fields': ['notify_email', 'notify_on_error', 'notify_on_critical'],
        }),
        ('Технические поля (только чтение)', {
            'fields': ['connect_token', 'connect_token_expires_at'],
            'description': (
                'Временный токен для привязки Telegram через бота. '
                'Заполняется при нажатии «Подключить Telegram» в дашборде, '
                'очищается после успешной привязки.'
            ),
            'classes': ['collapse'],
        }),
    ]


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(TenantScopedReadOnlyAdminMixin):
    """Read-only audit of per-channel external deliveries."""

    list_display = [
        'id', 'tenant', 'event_key', 'channel', 'status', 'attempts', 'updated_at',
    ]
    list_filter = ['channel', 'status']
    search_fields = ['id', 'tenant__slug', 'event_key']
    readonly_fields = [field.name for field in NotificationDelivery._meta.fields]
