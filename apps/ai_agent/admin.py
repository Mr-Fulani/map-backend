from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.ai_agent.models import (
    AIModel, AIProviderPrice, AIRequestLog, TenantAISettings, TenantAITaskModel,
)


@admin.register(AIModel)
class AIModelAdmin(ModelAdmin):
    list_display = [
        'display_name', 'provider', 'external_id', 'quality_tier',
        'speed_tier', 'is_active', 'is_pricing_verified',
        'is_configured', 'is_default', 'is_fallback',
    ]
    list_filter = [
        'provider', 'quality_tier', 'speed_tier',
        'is_active', 'is_pricing_verified',
    ]
    search_fields = ['display_name', 'external_id']


@admin.register(AIProviderPrice)
class AIProviderPriceAdmin(ModelAdmin):
    list_display = [
        'model', 'currency', 'input_per_million',
        'cached_read_per_million', 'cached_write_per_million',
        'output_per_million', 'effective_from', 'created_at',
    ]
    list_filter = ['currency', 'model__provider']
    search_fields = ['model__display_name', 'model__external_id']
    readonly_fields = ['created_at']
    date_hierarchy = 'effective_from'

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [
                'model', 'currency', 'input_per_million',
                'cached_read_per_million', 'cached_write_per_million',
                'output_per_million', 'effective_from', 'source_url',
                'notes', 'created_at',
            ]
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TenantAISettings)
class TenantAISettingsAdmin(ModelAdmin):
    list_display = ['tenant', 'default_model', 'use_task_overrides', 'updated_at']
    search_fields = ['tenant__name', 'tenant__slug']


@admin.register(TenantAITaskModel)
class TenantAITaskModelAdmin(ModelAdmin):
    list_display = ['settings', 'task_type', 'model']
    list_filter = ['task_type']


@admin.register(AIRequestLog)
class AIRequestLogAdmin(ModelAdmin):
    list_display = [
        'tenant', 'task_type', 'provider', 'model_id', 'status',
        'charged_credits', 'duration_ms', 'created_at',
    ]
    list_filter = ['task_type', 'provider', 'status']
    search_fields = ['tenant__name', 'tenant__slug', 'model_id', 'error_code']
    readonly_fields = [
        'tenant', 'task_type', 'provider', 'model_id', 'status',
        'input_tokens', 'cached_input_tokens', 'output_tokens',
        'charged_credits', 'duration_ms', 'error_code', 'error_message',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
