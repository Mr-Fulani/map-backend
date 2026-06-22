from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

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


class BrowserSessionInline(TabularInline):
    """Инлайн браузерной сессии в карточке аккаунта."""

    model = None  # устанавливается ниже после импорта
    extra = 0
    max_num = 1
    can_delete = False
    readonly_fields = [
        'browser_type', 'profile_dir', 'fingerprint',
        'session_valid', 'sms_pending', 'last_login_at', 'updated_at',
    ]
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MarketplaceAccount)
class MarketplaceAccountAdmin(ModelAdmin):
    """Администрирование аккаунтов Avito тенантов."""

    list_display = ['name', 'tenant', 'marketplace', 'publish_method', 'is_active', 'external_id']
    list_filter = ['tenant', 'marketplace', 'publish_method', 'is_active']
    search_fields = ['name', 'tenant__slug']
    readonly_fields = ['credentials_enc', 'web_login_enc', 'web_password_enc', 'created_at', 'updated_at']

    def get_inlines(self, request, obj=None):
        """Показываем BrowserSession инлайн только для web-аккаунтов."""
        from apps.browser_sessions.models import BrowserSession
        if obj and obj.publish_method == MarketplaceAccount.PUBLISH_WEB:
            BrowserSessionInline.model = BrowserSession
            return [BrowserSessionInline]
        return []


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
