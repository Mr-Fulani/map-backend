from django.contrib import admin

from apps.image_search.models import ImageSearchCache, ImageSearchLog


@admin.register(ImageSearchLog)
class ImageSearchLogAdmin(admin.ModelAdmin):
    """Админка логов поиска изображений."""

    list_display = ('product', 'source_id', 'query', 'results_count', 'accepted_count',
                    'duration_ms', 'created_at')
    list_filter = ('source_id', 'confidence', 'created_at')
    search_fields = ('query', 'product__article', 'product__name')
    readonly_fields = ('tenant', 'product', 'source_id', 'query', 'confidence',
                       'results_count', 'accepted_count', 'duration_ms', 'error',
                       'query_metrics', 'query_builder_version', 'created_at')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ImageSearchCache)
class ImageSearchCacheAdmin(admin.ModelAdmin):
    """Админка кеша поиска."""

    list_display = ('cache_key', 'expires_at', 'created_at')
    search_fields = ('cache_key',)
    readonly_fields = ('cache_key', 'results', 'expires_at', 'created_at')
