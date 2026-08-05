from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun,
)


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
class WebResearchRunAdmin(ModelAdmin):
    list_display = [
        'id', 'product', 'tenant', 'status', 'trigger', 'search_provider',
        'result_count', 'claim_count', 'created_at',
    ]
    list_filter = ['status', 'trigger', 'search_provider', 'ai_provider', 'created_at']
    search_fields = ['product__article', 'product__name', 'tenant__name', 'queries']
    readonly_fields = [
        'tenant', 'product', 'status', 'trigger', 'search_provider',
        'ai_provider', 'ai_model', 'queries', 'coverage_before', 'coverage_after',
        'result_count', 'claim_count', 'generate_after', 'error_message',
        'started_at', 'finished_at', 'created_at', 'updated_at',
    ]
    inlines = [EvidenceInline, ClaimInline]

    def has_add_permission(self, request):
        return False


@admin.register(WebResearchEvidence)
class WebResearchEvidenceAdmin(ModelAdmin):
    list_display = ['id', 'run', 'domain', 'rank', 'title', 'created_at']
    list_filter = ['domain', 'created_at']
    search_fields = ['url', 'title', 'query', 'run__product__article']
    readonly_fields = [
        'run', 'query', 'rank', 'title', 'url', 'domain', 'snippet',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False


@admin.register(WebResearchClaim)
class WebResearchClaimAdmin(ModelAdmin):
    list_display = [
        'id', 'run', 'claim_type', 'confidence', 'saved_model', 'saved_record_id',
    ]
    list_filter = ['claim_type', 'created_at']
    search_fields = ['run__product__article', 'run__product__name']
    readonly_fields = [
        'run', 'claim_type', 'payload', 'confidence', 'evidence',
        'saved_model', 'saved_record_id', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False
