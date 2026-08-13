from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.conf import settings
from django.db import close_old_connections, connections
from django.test import Client, RequestFactory
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.middleware import TenantMiddleware
from apps.tenants.jwt_serializers import TenantTokenObtainPairSerializer
from apps.tenants.models import Tenant, TenantUser
from apps.tenants.session_tokens import _claim_id, rotate_refresh_token
from apps.tenants.services import TenantService


def _session(slug='session', email='session@example.com', password='CorrectHorse-123'):
    tenant, _ = TenantService.create_tenant('Session Corp', slug, email, password)
    membership = TenantUser.objects.select_related('user', 'tenant').get(tenant=tenant)
    membership.user._current_tenant_membership = membership
    refresh = TenantTokenObtainPairSerializer.get_token(membership.user)
    return tenant, membership.user, str(refresh), str(refresh.access_token)


def test_claim_id_accepts_simplejwt_string_ids_and_legacy_integers():
    assert _claim_id({'user_id': '42'}, 'user_id') == 42
    assert _claim_id({'user_id': 42}, 'user_id') == 42
    assert _claim_id({'user_id': '9223372036854775807'}, 'user_id') == 2**63 - 1


@pytest.mark.parametrize(
    'value',
    (
        True,
        False,
        0,
        -1,
        2**63,
        10**100,
        '',
        '0',
        '-1',
        '+1',
        '01',
        '1.0',
        '１',
        '9223372036854775808',
        '9' * 21,
    ),
)
def test_claim_id_rejects_noncanonical_or_unbounded_values(value):
    with pytest.raises(InvalidToken):
        _claim_id({'user_id': value}, 'user_id')


@pytest.mark.django_db
def test_tokens_include_auth_version_and_access_rejects_revoked_session():
    _, user, _, access = _session()
    token = RefreshToken.for_user(user)
    assert 'auth_version' not in token

    client = Client(HTTP_AUTHORIZATION=f'Bearer {access}')
    assert client.get('/api/v1/auth/me/').status_code == 200
    assert client.post('/api/v1/auth/logout-all/').status_code == 204
    assert client.get('/api/v1/auth/me/').status_code == 401


@pytest.mark.django_db
def test_refresh_rotation_is_one_time_and_persists_successor():
    _, _, refresh, _ = _session(slug='rotate', email='rotate@example.com')
    first = Client().post(
        '/api/v1/auth/token/refresh/',
        {'refresh': refresh},
        content_type='application/json',
    )
    assert first.status_code == 200
    assert set(first.json()) == {'access', 'refresh', 'browser_session_id'}
    browser_session_id = first.json()['browser_session_id']
    assert browser_session_id == RefreshToken(refresh, verify=False)['sid']
    consumed_jti = RefreshToken(refresh, verify=False)['jti']
    assert BlacklistedToken.objects.filter(token__jti=consumed_jti).exists()

    replay = Client().post(
        '/api/v1/auth/token/refresh/',
        {'refresh': refresh},
        content_type='application/json',
    )
    assert replay.status_code == 401

    successor = Client().post(
        '/api/v1/auth/token/refresh/',
        {'refresh': first.json()['refresh']},
        content_type='application/json',
    )
    assert successor.status_code == 200
    assert successor.json()['browser_session_id'] == browser_session_id
    assert RefreshToken(first.json()['refresh'], verify=False)['sid'] == browser_session_id


@pytest.mark.django_db
def test_refresh_rotation_upgrades_legacy_token_with_stable_session_id():
    _, _, refresh, _ = _session(slug='legacy-sid', email='legacy-sid@example.com')
    legacy = RefreshToken(refresh)
    legacy_session_id = legacy['jti']
    del legacy['sid']

    first = rotate_refresh_token(str(legacy))

    assert first['browser_session_id'] == legacy_session_id
    successor = RefreshToken(first['refresh'], verify=False)
    assert successor['sid'] == legacy_session_id
    second = rotate_refresh_token(first['refresh'])
    assert second['browser_session_id'] == legacy_session_id


@pytest.mark.django_db(transaction=True)
def test_concurrent_refresh_allows_exactly_one_winner():
    _, _, refresh, _ = _session(slug='race', email='race@example.com')
    barrier = Barrier(2)

    def rotate():
        close_old_connections()
        barrier.wait()
        try:
            rotate_refresh_token(refresh)
            return 'ok'
        except InvalidToken:
            return 'rejected'
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: rotate(), range(2)))
    assert sorted(results) == ['ok', 'rejected']


@pytest.mark.django_db
def test_cli_logout_rejects_refresh_owned_by_another_user():
    _, _, first_refresh, first_access = _session(slug='owner-a', email='a@example.com')
    _, _, second_refresh, _ = _session(slug='owner-b', email='b@example.com')
    client = Client(HTTP_AUTHORIZATION=f'Bearer {first_access}')
    response = client.post(
        '/api/v1/auth/logout/',
        {'refresh': second_refresh},
        content_type='application/json',
    )
    assert response.status_code == 401
    assert first_refresh != second_refresh


@pytest.mark.django_db
def test_login_requires_tenant_slug_when_user_has_multiple_memberships():
    tenant, user, _, _ = _session(slug='first-login', email='multi-login@example.com')
    second = Tenant.objects.create(name='Second', slug='second-login')
    TenantUser.objects.create(user=user, tenant=second, role=TenantUser.ROLE_OPERATOR)

    response = Client().post('/api/v1/auth/token/', {
        'email': user.email,
        'password': 'CorrectHorse-123',
    }, content_type='application/json')
    assert response.status_code == 400
    assert 'tenant_slug' in str(response.json())

    selected = Client().post('/api/v1/auth/token/', {
        'email': user.email,
        'password': 'CorrectHorse-123',
        'tenant_slug': tenant.slug,
    }, content_type='application/json')
    assert selected.status_code == 200


@pytest.mark.django_db
def test_bearer_without_tenant_claim_never_falls_back_to_host():
    tenant, user, _, _ = _session(slug='host-tenant', email='host@example.com')
    raw_access = str(RefreshToken.for_user(user).access_token)
    request = RequestFactory().get(
        '/api/v1/auth/me/',
        HTTP_HOST=f'{tenant.slug}.example.test',
        HTTP_AUTHORIZATION=f'Bearer {raw_access}',
    )
    middleware = TenantMiddleware(lambda incoming: None)
    assert middleware._resolve_tenant(request) is None


@pytest.mark.django_db
def test_browser_session_is_csrf_protected_and_hides_refresh_token():
    _session(slug='browser', email='browser@example.com')
    client = Client(enforce_csrf_checks=True)

    rejected = client.post('/api/v1/auth/browser/login/', {
        'email': 'browser@example.com',
        'password': 'CorrectHorse-123',
    }, content_type='application/json')
    assert rejected.status_code == 403
    assert rejected.json() == {
        'status': 'error',
        'code': 'csrf_failed',
        'message': 'CSRF-проверка не пройдена. Получите новый CSRF-токен.',
    }
    assert 'no-store' in rejected['Cache-Control']

    csrf = client.get('/api/v1/auth/browser/csrf/')
    csrf_token = csrf.json()['csrf_token']
    assert csrf.status_code == 200
    assert 'no-store' in csrf['Cache-Control']

    login = client.post('/api/v1/auth/browser/login/', {
        'email': 'browser@example.com',
        'password': 'CorrectHorse-123',
    }, content_type='application/json', HTTP_X_CSRFTOKEN=csrf_token)
    assert login.status_code == 200
    assert 'access' in login.json()
    assert 'refresh' not in login.json()
    browser_session_id = login.json()['browser_session_id']
    assert isinstance(browser_session_id, str)
    assert len(browser_session_id) == 32
    cookie = login.cookies[settings.AUTH_REFRESH_COOKIE_NAME]
    assert cookie['httponly'] is True
    assert cookie['path'] == settings.AUTH_REFRESH_COOKIE_PATH
    assert 'no-store' in login['Cache-Control']

    rotated = client.post(
        '/api/v1/auth/browser/refresh/',
        content_type='application/json',
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert rotated.status_code == 200
    assert set(rotated.json()) == {'access', 'browser_session_id'}
    assert rotated.json()['browser_session_id'] == browser_session_id
    assert settings.AUTH_REFRESH_COOKIE_NAME in rotated.cookies

    logout = client.post(
        '/api/v1/auth/browser/logout/',
        content_type='application/json',
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert logout.status_code == 204
    assert logout.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value == ''


@pytest.mark.django_db
@pytest.mark.parametrize(
    'refresh_cookie',
    (None, 'not-a-valid-refresh-token', 'x' * 4097),
)
def test_browser_refresh_rejects_missing_or_invalid_cookie_as_signed_out(
    refresh_cookie,
):
    client = Client(enforce_csrf_checks=True)
    csrf_rejected = client.post(
        '/api/v1/auth/browser/refresh/',
        content_type='application/json',
    )
    assert csrf_rejected.status_code == 403
    assert csrf_rejected.json()['code'] == 'csrf_failed'

    csrf_token = client.get('/api/v1/auth/browser/csrf/').json()['csrf_token']
    if refresh_cookie is not None:
        client.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = refresh_cookie

    response = client.post(
        '/api/v1/auth/browser/refresh/',
        content_type='application/json',
        HTTP_X_CSRFTOKEN=csrf_token,
    )

    assert response.status_code == 401
    assert response.json() == {
        'status': 'error',
        'code': 'unauthorized',
        'message': 'Сессия истекла. Войдите снова.',
    }
    cleared = response.cookies[settings.AUTH_REFRESH_COOKIE_NAME]
    assert cleared.value == ''
    assert cleared['path'] == settings.AUTH_REFRESH_COOKIE_PATH
    assert int(cleared['max-age']) == 0
    assert 'no-store' in response['Cache-Control']


@pytest.mark.django_db
def test_browser_logout_all_revokes_access_without_authorization_header():
    _session(slug='browser-all', email='browser-all@example.com')
    client = Client(enforce_csrf_checks=True)
    csrf_token = client.get('/api/v1/auth/browser/csrf/').json()['csrf_token']
    login = client.post('/api/v1/auth/browser/login/', {
        'email': 'browser-all@example.com',
        'password': 'CorrectHorse-123',
    }, content_type='application/json', HTTP_X_CSRFTOKEN=csrf_token)
    access = login.json()['access']

    logout = client.post(
        '/api/v1/auth/browser/logout-all/',
        content_type='application/json',
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert logout.status_code == 204
    assert Client(
        HTTP_AUTHORIZATION=f'Bearer {access}',
    ).get('/api/v1/auth/me/').status_code == 401
