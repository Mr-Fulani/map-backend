"""Explicit authentication helpers for API tests.

Tests must choose a machine operator or a human owner deliberately; the
registration key is intentionally minimal and must not be upgraded implicitly.
"""

from django.test import Client

from apps.tenants.jwt_serializers import TenantTokenObtainPairSerializer
from apps.tenants.models import APIKey, API_KEY_SCOPES, TenantUser
from apps.tenants.services import TenantService


def create_operator_key(tenant, *, scopes=None, name='Test Operator Key'):
    """Return an explicit machine credential for integration endpoint tests."""
    _, plaintext = APIKey.generate(
        tenant,
        name,
        role=APIKey.ROLE_OPERATOR,
        scopes=all_machine_scopes() if scopes is None else scopes,
    )
    return plaintext


def all_machine_scopes():
    return sorted(API_KEY_SCOPES)


def create_tenant_with_operator_key(
    name,
    slug,
    owner_email,
    owner_password,
):
    tenant, _ = TenantService.create_tenant(
        name,
        slug,
        owner_email,
        owner_password,
    )
    return tenant, create_operator_key(tenant)


def membership_access_token(membership):
    """Create a tenant-bound JWT for the provided current membership."""
    user = membership.user
    user._current_tenant_membership = membership
    refresh = TenantTokenObtainPairSerializer.get_token(user)
    return str(refresh.access_token)


def owner_access_token(tenant):
    """Create a tenant-bound JWT for tests of human-only endpoints."""
    membership = TenantUser.objects.select_related('user', 'tenant').get(
        tenant=tenant,
        role=TenantUser.ROLE_OWNER,
    )
    return membership_access_token(membership)


def owner_client(tenant):
    return Client(HTTP_AUTHORIZATION=f'Bearer {owner_access_token(tenant)}')
