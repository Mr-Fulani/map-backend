from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.marketplaces.models import AvitoCategory, CategoryMapping, Listing, MarketplaceAccount


@admin.register(Listing)
class ListingAdmin(ModelAdmin):
    """
    Администрирование листингов Avito.

    Показывает статус, ссылку на объявление, причину отклонения и счётчик ретраев.
    Все идентификаторы Avito доступны только для чтения.
    """

    list_display = [
        'title', 'tenant', 'status', 'external_url_link',
        'rejection_reason', 'retry_count', 'published_at',
    ]
    list_filter = ['tenant', 'status', 'account']
    search_fields = ['title', 'external_id', 'tenant__slug']
    readonly_fields = [
        'external_id', 'external_url', 'publish_idempotency_key',
        'published_at', 'last_sync_at', 'created_at', 'updated_at',
    ]

    @admin.display(description='Ссылка Avito')
    def external_url_link(self, obj):
        """Возвращает HTML-ссылку на объявление Avito."""
        from django.utils.html import format_html

        if obj.external_url:
            return format_html('<a href="{}" target="_blank">открыть</a>', obj.external_url)
        return '—'


@admin.register(MarketplaceAccount)
class MarketplaceAccountAdmin(ModelAdmin):
    """Администрирование аккаунтов Avito тенантов."""

    list_display = ['name', 'tenant', 'marketplace', 'is_active', 'external_id']
    list_filter = ['tenant', 'marketplace', 'is_active']
    search_fields = ['name', 'tenant__slug']
    readonly_fields = ['credentials_enc', 'created_at', 'updated_at']


@admin.register(AvitoCategory)
class AvitoCategoryAdmin(ModelAdmin):
    """Справочник категорий Avito."""

    list_display = ['avito_id', 'name', 'parent_id']
    search_fields = ['name', 'avito_id']


@admin.register(CategoryMapping)
class CategoryMappingAdmin(ModelAdmin):
    """Маппинг категорий источника данных на категории Avito."""

    list_display = ['tenant', 'marketplace', 'category_source', 'category_target', 'category_id']
    list_filter = ['tenant', 'marketplace']
    search_fields = ['category_source', 'tenant__slug']
