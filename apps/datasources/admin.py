from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import DataSourceConnection


@admin.register(DataSourceConnection)
class DataSourceConnectionAdmin(ModelAdmin):
    """Администрирование источников данных для импорта товаров."""

    list_display = ['name', 'tenant', 'type', 'is_active', 'last_sync_status', 'last_sync_at']
    list_filter = ['type', 'is_active', 'last_sync_status']
    search_fields = ['name', 'tenant__name', 'tenant__slug']
    readonly_fields = ['last_sync_at', 'last_sync_status', 'last_error']
    exclude = ['credentials']
    fieldsets = [
        ('Основное', {
            'fields': ['tenant', 'name', 'type', 'is_active'],
        }),
        ('Статус синхронизации', {
            'fields': ['last_sync_status', 'last_sync_at', 'last_error'],
            'description': 'Заполняется автоматически при синхронизации.',
        }),
    ]
