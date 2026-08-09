from rest_framework.permissions import BasePermission, SAFE_METHODS

from apps.tenants.models import TenantUser


def _membership(request):
    """Возвращает и кеширует членство пользователя в текущем tenant-е."""
    if hasattr(request, '_tenant_membership'):
        return request._tenant_membership

    tenant = getattr(request, 'tenant', None)
    user = getattr(request, 'user', None)
    membership = None
    if tenant is not None and user is not None and user.is_authenticated:
        membership = TenantUser.objects.filter(
            tenant=tenant,
            user=user,
        ).first()
    request._tenant_membership = membership
    return membership


class TenantRolePermission(BasePermission):
    """Проверяет актуальное членство и запрещает Viewer изменять данные."""

    message = 'Недостаточно прав для выполнения операции.'

    def has_permission(self, request, view):
        membership = _membership(request)
        if membership is None:
            return False
        if request.method in SAFE_METHODS:
            return True
        return membership.can_publish()


class TenantAdminPermission(BasePermission):
    """Доступ только владельцу и администратору tenant-а."""

    message = 'Операция доступна только владельцу или администратору.'

    def has_permission(self, request, view):
        membership = _membership(request)
        return bool(membership and membership.can_manage_connections())


class TenantAdminWritePermission(BasePermission):
    """Чтение участникам tenant-а, изменение — Owner/Admin."""

    message = 'Изменять настройки может только владелец или администратор.'

    def has_permission(self, request, view):
        membership = _membership(request)
        if membership is None:
            return False
        if request.method in SAFE_METHODS:
            return True
        return membership.can_manage_connections()


class TenantOwnerPermission(BasePermission):
    """Доступ только владельцу tenant-а."""

    message = 'Операция доступна только владельцу организации.'

    def has_permission(self, request, view):
        membership = _membership(request)
        return bool(membership and membership.can_manage_billing())
