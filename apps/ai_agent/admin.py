from django.contrib import admin, messages
from django.db import transaction
from unfold.admin import ModelAdmin

from apps.core.admin import TenantScopedReadOnlyAdminMixin
from apps.ai_agent.models import (
    AIModel, AIPromptTemplate, AIProviderOperation, AIProviderPrice, AIRequestLog,
    TenantAISettings, TenantAITaskModel,
)


@admin.register(AIPromptTemplate)
class AIPromptTemplateAdmin(ModelAdmin):
    list_display = [
        'name', 'task_type', 'catalog_domain', 'marketplace', 'version',
        'is_active', 'created_at',
    ]
    list_filter = ['task_type', 'catalog_domain', 'marketplace', 'is_active']
    search_fields = ['name', 'system_prompt', 'change_notes']
    readonly_fields = ['created_at', 'updated_at']
    actions = ['activate_selected_version']

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [
                'task_type', 'catalog_domain', 'marketplace', 'version', 'name',
                'system_prompt', 'output_schema', 'change_notes',
                'created_at', 'updated_at',
            ]
        return self.readonly_fields

    @admin.action(description='Активировать выбранную версию промпта')
    def activate_selected_version(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'Выберите ровно одну версию.', level=messages.ERROR)
            return
        prompt = queryset.get()
        with transaction.atomic():
            type(prompt).objects.filter(
                task_type=prompt.task_type,
                catalog_domain=prompt.catalog_domain,
                marketplace=prompt.marketplace,
                is_active=True,
            ).exclude(pk=prompt.pk).update(is_active=False)
            prompt.is_active = True
            prompt.save(update_fields=['is_active', 'updated_at'])
        self.message_user(request, f'Активирован {prompt}.')

    def has_delete_permission(self, request, obj=None):
        return False


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
class AIRequestLogAdmin(TenantScopedReadOnlyAdminMixin):
    list_display = [
        'tenant', 'task_type', 'provider', 'model_id', 'status',
        'prompt_version', 'charged_credits', 'duration_ms', 'created_at',
    ]
    list_filter = ['task_type', 'provider', 'status']
    search_fields = ['tenant__name', 'tenant__slug', 'model_id', 'error_code']
    readonly_fields = [
        'tenant', 'task_type', 'provider', 'model_id', 'status',
        'input_tokens', 'cached_input_tokens', 'output_tokens',
        'charged_credits', 'duration_ms', 'error_code', 'error_message',
        'prompt_template', 'prompt_version', 'prompt_hash',
        'created_at', 'updated_at',
    ]


@admin.register(AIProviderOperation)
class AIProviderOperationAdmin(TenantScopedReadOnlyAdminMixin):
    """Read-only queue and audit trail for paid-provider reconciliation."""

    list_display = [
        'id', 'tenant', 'task_type', 'provider', 'model_id', 'status',
        'reserved_amount', 'charged_amount', 'uncertainty_marked_at',
        'network_started_at', 'apply_state', 'applied_at',
        'resolved_at', 'created_at',
    ]
    list_filter = ['status', 'task_type', 'provider', 'domain_type']
    search_fields = [
        'id', 'tenant__name', 'tenant__slug', 'model_id',
        'domain_reference', 'reservation_key',
    ]
    readonly_fields = [
        'id', 'tenant', 'task_type', 'provider', 'model_id',
        'reservation_key', 'reserved_amount', 'charged_amount',
        'domain_type', 'domain_reference', 'status', 'provider_error_code',
        'terminal_reason', 'resolution_action', 'operator_note',
        'validated_result', 'apply_state', 'applied_at',
        'network_started_at', 'uncertainty_marked_at',
        'released_at', 'settled_at', 'resolved_at',
        'created_at', 'updated_at',
    ]
