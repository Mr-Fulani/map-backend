from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import DataSourceConnection


@admin.register(DataSourceConnection)
class DataSourceConnectionAdmin(ModelAdmin):
    list_display = ['name', 'tenant', 'type', 'is_active', 'last_sync_status', 'last_sync_at']
    list_filter = ['type', 'is_active', 'last_sync_status']
    search_fields = ['name', 'tenant__name']
    readonly_fields = ['last_sync_at', 'last_sync_status', 'last_error']
    exclude = ['credentials']  # Не показываем зашифрованные креды в админке, чтобы не ломался шаблон
