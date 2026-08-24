from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.marketplaces.models import (
    AvitoAccountStatus,
    AvitoCategory,
    AvitoCategoryTreeSnapshot,
    CategoryMapping,
    Listing,
    MarketplaceAccount,
    MarketplaceFeedRun,
)


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


@admin.register(MarketplaceFeedRun)
class MarketplaceFeedRunAdmin(ModelAdmin):
    """Read-only diagnostics for durable provider feed generations."""

    list_display = [
        'id', 'account', 'marketplace', 'state', 'revision',
        'submission_reconcile_attempt', 'total_count', 'published_count',
        'rejected_count', 'pending_count', 'next_attempt_at', 'finished_at',
    ]
    list_filter = ['marketplace', 'state']
    search_fields = [
        'id', 'account__name', 'account__external_id',
        'tenant__slug', 'provider_run_id',
    ]
    readonly_fields = [field.name for field in MarketplaceFeedRun._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AvitoAccountStatus)
class AvitoAccountStatusAdmin(ModelAdmin):
    """Диагностика последней проверки подключения и тарифа Avito."""

    list_display = [
        'account', 'tenant', 'connection_status', 'autoload_status',
        'tariff_status', 'tariff_ends_at', 'last_attempted_at',
    ]
    list_filter = [
        'tenant', 'connection_status', 'autoload_status', 'tariff_status',
    ]
    search_fields = ['account__name', 'account__external_id', 'tenant__slug']
    readonly_fields = [
        'tenant', 'account', 'connection_status', 'autoload_status',
        'feed_configured', 'profile_checked_at', 'tariff_status',
        'tariff_name', 'tariff_started_at', 'tariff_ends_at',
        'tariff_price', 'placement_packages', 'scheduled_tariff',
        'tariff_checked_at', 'last_attempted_at', 'last_error_code',
        'last_error_message', 'notification_state', 'created_at', 'updated_at',
    ]


@admin.register(AvitoCategoryTreeSnapshot)
class AvitoCategoryTreeSnapshotAdmin(ModelAdmin):
    """Диагностика автоматического обновления дерева категорий Avito."""

    list_display = [
        'domain_slug', 'status', 'node_count', 'change_count',
        'fetched_at', 'applied_at', 'source_account',
    ]
    list_filter = ['status', 'domain_slug']
    readonly_fields = [
        'domain_slug', 'root_name', 'tree', 'checksum', 'status',
        'node_count', 'change_count', 'fetched_at', 'applied_at',
        'last_error', 'metadata', 'source_account', 'created_at', 'updated_at',
    ]


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
