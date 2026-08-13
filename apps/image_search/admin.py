from django.contrib import admin

from apps.core.admin import (
    SuperuserReadOnlyAdminMixin, TenantScopedReadOnlyAdminMixin,
)
from apps.image_search.models import (
    ImageSearchCache,
    ImageSearchLog,
    ImageSearchTask,
)


@admin.register(ImageSearchLog)
class ImageSearchLogAdmin(TenantScopedReadOnlyAdminMixin):
    """Админка логов поиска изображений."""

    list_display = (
        'product', 'source_id', 'query', 'outcome', 'results_count',
        'accepted_count', 'duration_ms', 'created_at',
    )
    list_filter = ('source_id', 'outcome', 'confidence', 'created_at')
    search_fields = ('query', 'product__article', 'product__name')
    readonly_fields = ('tenant', 'product', 'source_id', 'query', 'confidence',
                       'results_count', 'accepted_count', 'duration_ms', 'outcome',
                       'error_code', 'error',
                       'query_metrics', 'query_builder_version',
                       'workflow_key', 'workflow_slot', 'created_at')
    date_hierarchy = 'created_at'


@admin.register(ImageSearchCache)
class ImageSearchCacheAdmin(SuperuserReadOnlyAdminMixin):
    """Админка кеша поиска."""

    list_display = ('cache_key', 'expires_at', 'created_at')
    search_fields = ('cache_key',)
    readonly_fields = ('cache_key', 'results', 'expires_at', 'created_at')


@admin.register(ImageSearchTask)
class ImageSearchTaskAdmin(TenantScopedReadOnlyAdminMixin):
    list_display = (
        'task_id', 'tenant', 'product', 'status', 'finished_at', 'updated_at',
    )
    list_filter = ('status', 'created_at', 'finished_at')
    search_fields = ('task_id', 'product__article', 'tenant__slug')
    readonly_fields = (
        'tenant', 'product', 'task_id', 'dispatch', 'intent', 'status',
        'result', 'error_code', 'error_message', 'finished_at',
        'created_at', 'updated_at',
    )
