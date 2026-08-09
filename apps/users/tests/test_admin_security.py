import pytest
from django.contrib.admin.sites import AdminSite
from django.test import Client, RequestFactory

from apps.tenants.jwt_serializers import TenantTokenObtainPairSerializer
from apps.tenants.models import TenantUser
from apps.tenants.services import TenantService
from apps.users.admin import SessionRevokingAdminPasswordChangeForm, UserAdmin
from apps.users.models import User


PASSWORD = 'CorrectHorse-123'
NEW_PASSWORD = 'DifferentHorse-456'


def _session(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@example.com',
        PASSWORD,
    )
    membership = TenantUser.objects.select_related('user').get(tenant=tenant)
    membership.user._current_tenant_membership = membership
    refresh = TenantTokenObtainPairSerializer.get_token(membership.user)
    return membership.user, str(refresh), str(refresh.access_token)


@pytest.mark.django_db
def test_admin_password_reset_revokes_existing_jwt_sessions():
    user, refresh, access = _session('admin-password-reset')
    form = SessionRevokingAdminPasswordChangeForm(user, {
        'password1': NEW_PASSWORD,
        'password2': NEW_PASSWORD,
    })

    assert form.is_valid(), form.errors
    form.save()

    user.refresh_from_db()
    assert user.auth_version == 2
    assert user.check_password(NEW_PASSWORD)
    assert Client().post(
        '/api/v1/auth/token/refresh/',
        {'refresh': refresh},
        content_type='application/json',
    ).status_code == 401
    assert Client(HTTP_AUTHORIZATION=f'Bearer {access}').get(
        '/api/v1/auth/me/',
    ).status_code == 401


@pytest.mark.django_db
def test_admin_identity_change_revokes_sessions_before_possible_reactivation():
    user, refresh, _access = _session('admin-identity-change')
    model_admin = UserAdmin(User, AdminSite())
    request = RequestFactory().post('/admin/users/user/')
    user.email = 'changed-by-admin@example.com'

    model_admin.save_model(request, user, form=None, change=True)

    user.refresh_from_db()
    assert user.auth_version == 2
    assert Client().post(
        '/api/v1/auth/token/refresh/',
        {'refresh': refresh},
        content_type='application/json',
    ).status_code == 401
