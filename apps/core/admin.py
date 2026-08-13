from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.core.models import (
    BackgroundJobDispatch, PaidIngressIntent, TenantDailyPaidUsage,
)


class TenantScopedAdminMixin(ModelAdmin):
    """Scope a tenant-owned admin model to the staff user's memberships."""

    tenant_lookup = 'tenant_id'

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        tenant_ids = request.user.tenant_memberships.values_list('tenant_id', flat=True)
        return queryset.filter(**{f'{self.tenant_lookup}__in': tenant_ids})

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # A scoped changelist alone is insufficient for an editable admin:
        # a crafted add/change POST could otherwise submit another tenant ID.
        if db_field.name == 'tenant' and not request.user.is_superuser:
            from apps.tenants.models import Tenant
            kwargs['queryset'] = Tenant.objects.filter(
                members__user=request.user,
            ).distinct()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class TenantScopedReadOnlyAdminMixin(TenantScopedAdminMixin):
    """Read-only admin journal scoped to a staff user's tenant memberships."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SuperuserReadOnlyAdminMixin(ModelAdmin):
    """Hide a global operational journal that has no tenant ownership link."""

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if request.user.is_superuser else queryset.none()

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BackgroundJobDispatch)
class BackgroundJobDispatchAdmin(SuperuserReadOnlyAdminMixin):
    """Read-only operational journal for durable user jobs."""

    list_display = [
        'created_at', 'task_name', 'queue', 'status', 'run_attempts',
        'available_at', 'lease_expires_at', 'finished_at',
    ]
    list_filter = ['status', 'queue', 'task_name', 'created_at']
    search_fields = ['id', 'deduplication_key', 'last_error']
    readonly_fields = [field.name for field in BackgroundJobDispatch._meta.fields]


@admin.register(PaidIngressIntent)
class PaidIngressIntentAdmin(TenantScopedReadOnlyAdminMixin):
    list_display = [
        'created_at', 'tenant', 'operation', 'resource_type', 'resource_id',
        'result_type', 'result_id',
    ]
    list_filter = ['operation', 'resource_type', 'result_type', 'created_at']
    search_fields = ['idempotency_key', 'resource_id', 'result_id']
    readonly_fields = [field.name for field in PaidIngressIntent._meta.fields]


@admin.register(TenantDailyPaidUsage)
class TenantDailyPaidUsageAdmin(TenantScopedReadOnlyAdminMixin):
    list_display = ['usage_date', 'tenant', 'scope', 'units', 'updated_at']
    list_filter = ['scope', 'usage_date']
    search_fields = ['tenant__name', 'tenant__slug']
    readonly_fields = [field.name for field in TenantDailyPaidUsage._meta.fields]
