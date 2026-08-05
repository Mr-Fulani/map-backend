class TenantScopedReadOnlyAdminMixin:
    """Read-only admin journal scoped to a staff user's tenant memberships."""

    tenant_lookup = 'tenant_id'

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        tenant_ids = request.user.tenant_memberships.values_list('tenant_id', flat=True)
        return queryset.filter(**{f'{self.tenant_lookup}__in': tenant_ids})

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
