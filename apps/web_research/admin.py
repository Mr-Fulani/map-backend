from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from apps.core.admin import TenantScopedReadOnlyAdminMixin
from apps.tenants.models import Tenant
from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun,
)


class ResearchTenantFilter(admin.SimpleListFilter):
    title = 'Тенант'
    parameter_name = 'tenant'

    def lookups(self, request, model_admin):
        tenants = Tenant.objects.filter(web_research_runs__isnull=False)
        if not request.user.is_superuser:
            tenants = tenants.filter(members__user=request.user)
        return [
            (str(tenant_id), name)
            for tenant_id, name in tenants.order_by('name').distinct().values_list('id', 'name')
        ]

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        lookup = (
            'tenant_id'
            if queryset.model is WebResearchRun
            else 'run__tenant_id'
        )
        return queryset.filter(**{lookup: self.value()})


class EvidenceInline(TabularInline):
    model = WebResearchEvidence
    extra = 0
    fields = ['rank', 'domain', 'title', 'url', 'query']
    readonly_fields = fields
    can_delete = False


class ClaimInline(TabularInline):
    model = WebResearchClaim
    extra = 0
    fields = ['claim_type', 'confidence', 'payload', 'saved_model', 'saved_record_id']
    readonly_fields = fields
    can_delete = False


@admin.register(WebResearchRun)
class WebResearchRunAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    list_display = [
        'id', 'product', 'tenant', 'status', 'trigger', 'search_provider',
        'result_count', 'claim_count', 'created_at',
    ]
    list_filter = [
        ResearchTenantFilter, 'status', 'trigger', 'search_provider',
        'ai_provider', 'created_at',
    ]
    search_fields = [
        'product__article', 'product__name', 'tenant__name', 'tenant__slug', 'queries',
    ]
    list_select_related = ['tenant', 'product']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'tenant', 'product', 'status', 'trigger', 'search_provider',
        'ai_provider', 'ai_model', 'queries', 'coverage_before', 'coverage_after',
        'result_count', 'claim_count', 'generate_after', 'error_message',
        'started_at', 'finished_at', 'created_at', 'updated_at',
    ]
    inlines = [EvidenceInline, ClaimInline]


@admin.register(WebResearchEvidence)
class WebResearchEvidenceAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    tenant_lookup = 'run__tenant_id'
    list_display = ['id', 'get_tenant', 'run', 'domain', 'rank', 'title', 'created_at']
    list_filter = [ResearchTenantFilter, 'domain', 'created_at']
    search_fields = [
        'url', 'title', 'query', 'run__product__article',
        'run__tenant__name', 'run__tenant__slug',
    ]
    list_select_related = ['run__tenant', 'run__product']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'run', 'query', 'rank', 'title', 'url', 'domain', 'snippet',
        'created_at', 'updated_at',
    ]

    @admin.display(description='Тенант', ordering='run__tenant__name')
    def get_tenant(self, obj):
        return obj.run.tenant


@admin.register(WebResearchClaim)
class WebResearchClaimAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    tenant_lookup = 'run__tenant_id'
    list_display = [
        'id', 'get_tenant', 'run', 'claim_type', 'confidence',
        'saved_model', 'saved_record_id',
    ]
    list_filter = [ResearchTenantFilter, 'claim_type', 'created_at']
    search_fields = [
        'run__product__article', 'run__product__name',
        'run__tenant__name', 'run__tenant__slug',
    ]
    list_select_related = ['run__tenant', 'run__product']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'run', 'claim_type', 'payload', 'confidence', 'evidence',
        'saved_model', 'saved_record_id', 'created_at', 'updated_at',
    ]

    @admin.display(description='Тенант', ordering='run__tenant__name')
    def get_tenant(self, obj):
        return obj.run.tenant
