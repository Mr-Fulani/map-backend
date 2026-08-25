from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.marketplaces.models import (
    AvitoAccountStatus,
    AvitoCategory,
    AvitoCategoryTreeSnapshot,
    CategoryMapping,
    Listing,
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedFetchEvidence,
    MarketplaceFeedPutReconciliationAudit,
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
        # Workflow state and feed-visible values must pass through the fenced
        # ListingService/task paths, never through generic admin writes.
        'tenant', 'product', 'account', 'feed_run', 'status',
        'external_id', 'external_url', 'publish_idempotency_key',
        'title', 'description_ai', 'price_on_listing', 'margin_pct',
        'ad_type', 'placement_address', 'address_override',
        'seller_address_id_override', 'manager_name_override',
        'contact_phone_override', 'bulk_address',
        'bulk_seller_address_id', 'bulk_manager_name',
        'bulk_contact_phone', 'bulk_placement_address',
        'rejection_reason', 'retry_count', 'next_retry_at',
        'published_at', 'last_sync_at', 'remote_status',
        'remote_status_checked_at', 'next_status_check_at',
        'status_check_claim_token', 'status_check_claimed_until',
        'deleted_at', 'created_at', 'updated_at',
    ]

    @admin.display(description='Ссылка Avito')
    def external_url_link(self, obj):
        """Возвращает HTML-ссылку на объявление Avito."""
        from django.utils.html import format_html

        if obj.external_url:
            return format_html('<a href="{}" target="_blank">открыть</a>', obj.external_url)
        return '—'

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MarketplaceAccount)
class MarketplaceAccountAdmin(ModelAdmin):
    """Read-only diagnostics; mutations use the fenced service path."""

    list_display = ['name', 'tenant', 'marketplace', 'is_active', 'external_id']
    list_filter = ['tenant', 'marketplace', 'is_active']
    search_fields = ['name', 'tenant__slug']
    readonly_fields = [field.name for field in MarketplaceAccount._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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


@admin.register(MarketplaceFeedEndpoint)
class MarketplaceFeedEndpointAdmin(ModelAdmin):
    """Read-only diagnostics for the dark stable-feed endpoint schema."""

    list_display = [
        'public_id', 'account', 'storage_mode', 'profile_state',
        'profile_revision', 'serve_enabled', 'profile_verified_at', 'updated_at',
    ]
    list_filter = ['storage_mode', 'profile_state', 'serve_enabled']
    search_fields = [
        'public_id', 'account__name', 'account__external_id',
        'account__tenant__slug',
    ]
    list_select_related = ['account', 'account__tenant']
    readonly_fields = [field.name for field in MarketplaceFeedEndpoint._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MarketplaceFeedArtifact)
class MarketplaceFeedArtifactAdmin(ModelAdmin):
    """Read-only metadata without exposing private object locators."""

    list_display = [
        'id', 'account', 'endpoint', 'run', 'upload_attempt',
        'listing_count', 'size_bytes', 'verified_at',
    ]
    list_filter = ['content_type', 'verification_method']
    search_fields = [
        'id', 'run__id', 'account__name', 'account__external_id',
        'account__tenant__slug',
    ]
    list_select_related = ['account', 'account__tenant', 'endpoint', 'run']
    exclude = ['storage_bucket', 'object_key', 'object_version_id']
    readonly_fields = [
        field.name
        for field in MarketplaceFeedArtifact._meta.fields
        if field.name not in {'storage_bucket', 'object_key', 'object_version_id'}
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MarketplaceFeedArtifactUploadAttempt)
class MarketplaceFeedArtifactUploadAttemptAdmin(ModelAdmin):
    """Read-only upload journal with all private object locators hidden."""

    list_display = [
        'id', 'account', 'endpoint', 'run', 'attempt_no', 'state',
        'put_resolution_source', 'revision', 'projection_count', 'size_bytes',
        'created_at', 'updated_at',
    ]
    list_filter = ['state', 'put_resolution_source', 'content_type']
    search_fields = [
        'id', 'run__id', 'account__name', 'account__external_id',
        'account__tenant__slug',
    ]
    list_select_related = ['account', 'account__tenant', 'endpoint', 'run']
    exclude = [
        'storage_bucket', 'expected_bucket_owner', 'object_key',
        'object_version_id',
    ]
    readonly_fields = [
        field.name
        for field in MarketplaceFeedArtifactUploadAttempt._meta.fields
        if field.name not in {
            'storage_bucket', 'expected_bucket_owner', 'object_key',
            'object_version_id',
        }
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MarketplaceFeedPutReconciliationAudit)
class MarketplaceFeedPutReconciliationAuditAdmin(ModelAdmin):
    """Read-only, locator-free evidence for reviewed PUT reconciliation."""

    list_display = [
        'id', 'attempt', 'outcome', 'to_state', 'pre_revision',
        'post_revision', 'version_id_captured', 'decision_at', 'created_at',
    ]
    list_filter = ['outcome', 'to_state', 'version_id_captured']
    search_fields = [
        'id', 'attempt__id', 'attempt__run__id',
        'attempt__account__name', 'attempt__account__external_id',
        'attempt__account__tenant__slug',
    ]
    list_select_related = [
        'attempt', 'attempt__account', 'attempt__account__tenant',
        'attempt__run',
    ]
    readonly_fields = [
        field.name
        for field in MarketplaceFeedPutReconciliationAudit._meta.fields
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MarketplaceFeedFetchEvidence)
class MarketplaceFeedFetchEvidenceAdmin(ModelAdmin):
    """Read-only redirect evidence without capability or storage material."""

    list_display = [
        'id', 'endpoint', 'artifact', 'request_method', 'redirect_status',
        'capability_revision', 'endpoint_revision', 'source_intent_revision',
        'run_revision', 'issued_at', 'redirect_expires_at',
    ]
    list_filter = ['request_method', 'redirect_status']
    search_fields = [
        'id', 'endpoint__public_id', 'artifact__id',
        'endpoint__account__name', 'endpoint__account__external_id',
        'endpoint__account__tenant__slug',
    ]
    list_select_related = [
        'endpoint', 'endpoint__account', 'endpoint__account__tenant', 'artifact',
    ]
    readonly_fields = [
        field.name for field in MarketplaceFeedFetchEvidence._meta.fields
    ]

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
    readonly_fields = [field.name for field in CategoryMapping._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
