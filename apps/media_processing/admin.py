from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.core.admin import TenantScopedAdminMixin
from apps.media_processing.models import (
    ImageAssessment,
    MediaProcessingJob,
    MediaProcessingPreset,
    MediaProviderPolicy,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.media_processing.protection import unresolved_media_job_q


@admin.register(MediaProviderPolicy)
class MediaProviderPolicyAdmin(ModelAdmin):
    list_display = ('display_name', 'provider_id', 'is_active', 'priority', 'requests_per_minute')
    list_filter = ('is_active',)
    search_fields = ('display_name', 'provider_id')


@admin.register(MediaProcessingPreset)
class MediaProcessingPresetAdmin(ModelAdmin):
    list_display = ('name', 'tenant', 'slug', 'is_default', 'is_active')
    list_filter = ('is_active', 'is_default', 'tenant')
    search_fields = ('name', 'slug', 'tenant__name')


@admin.register(TenantMediaSettings)
class TenantMediaSettingsAdmin(ModelAdmin):
    list_display = (
        'tenant', 'default_preset', 'auto_process_manual_uploads',
        'auto_process_approved_search', 'allow_generative_operations',
    )
    list_filter = (
        'auto_process_manual_uploads', 'auto_process_approved_search',
        'allow_generative_operations',
    )
    search_fields = ('tenant__name', 'tenant__slug')


@admin.register(MediaProcessingJob)
class MediaProcessingJobAdmin(TenantScopedAdminMixin):
    list_display = (
        'id', 'tenant', 'product_image', 'provider_id', 'status',
        'provider_response_state', 'charged_credits', 'created_at',
    )
    list_filter = ('status', 'provider_response_state', 'provider_id', 'created_at')
    search_fields = (
        'product_image__product__article', 'product_image__product__name',
        'provider_job_id', 'idempotency_key',
    )
    readonly_fields = (
        'tenant', 'product_image', 'provider_job_id', 'status', 'idempotency_key',
        'provider_metadata', 'estimated_credits', 'charged_credits', 'error_code',
        'error_message', 'started_at', 'finished_at', 'created_at', 'updated_at',
        'provider_response_digest', 'provider_response_status',
        'provider_response_state', 'provider_response_recorded_at',
        'provider_response_apply_claimed_at', 'provider_response_resolved_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        """Never expose bulk delete; allow a reconciled terminal row explicitly."""
        if not super().has_delete_permission(request, obj):
            return False
        if obj is None:
            return False
        return not MediaProcessingJob.objects.filter(
            pk=obj.pk,
        ).filter(unresolved_media_job_q()).exists()


@admin.register(ProductImageVariant)
class ProductImageVariantAdmin(ModelAdmin):
    list_display = (
        'id', 'tenant', 'product_image', 'provider_id', 'width', 'height',
        'is_active', 'created_at',
    )
    list_filter = ('is_active', 'provider_id', 'created_at')
    search_fields = ('product_image__product__article', 'sha256', 's3_key')
    readonly_fields = (
        'tenant', 'product_image', 'job', 'provider_id', 'operations', 'parameters',
        's3_key', 'content_type', 'width', 'height', 'file_size_kb', 'sha256',
        'created_at', 'updated_at',
    )


@admin.register(ImageAssessment)
class ImageAssessmentAdmin(ModelAdmin):
    list_display = (
        'id', 'tenant', 'product', 'source_id', 'provider_id', 'verdict',
        'score', 'created_at',
    )
    list_filter = ('verdict', 'source_id', 'provider_id', 'created_at')
    search_fields = ('product__article', 'product__name', 'source_url')
    readonly_fields = (
        'tenant', 'product', 'product_image', 'source_url', 'source_id',
        'provider_id', 'model_id', 'verdict', 'score', 'reason_codes', 'checks',
        'expected_product', 'raw_response', 'created_at', 'updated_at',
    )
