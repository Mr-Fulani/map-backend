from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils import timezone

from apps.billing.models import Subscription
from apps.tenants.models import TenantUser
from apps.tenants.services import TenantService
from apps.users.models import User
from apps.users.tasks import send_password_reset_email


PASSWORD = 'CorrectHorse-123'
NEW_PASSWORD = 'AnotherHorse-456'


def _human_session(slug='profile', email='profile@example.com'):
    tenant, _ = TenantService.create_tenant('Profile Corp', slug, email, PASSWORD)
    login = Client().post('/api/v1/auth/token/', {
        'email': email,
        'password': PASSWORD,
    }, content_type='application/json')
    assert login.status_code == 200
    return tenant, User.objects.get(email=email), login.json()


@pytest.mark.django_db
@pytest.mark.parametrize('raw_payload', ['[]', '"text"', 'null'])
def test_login_rejects_non_object_json_without_server_error(raw_payload):
    response = Client().generic(
        'POST',
        '/api/v1/auth/token/',
        data=raw_payload,
        content_type='application/json',
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_auth_serializers_bound_credentials_and_reset_identifiers():
    from apps.tenants.jwt_serializers import (
        TenantTokenObtainPairSerializer,
        TenantTokenRefreshSerializer,
    )
    from apps.users.serializers import PasswordResetConfirmSerializer

    login = TenantTokenObtainPairSerializer()
    assert login.fields['email'].max_length == 254
    assert login.fields['password'].max_length == 256
    assert TenantTokenRefreshSerializer().fields['refresh'].max_length == 4096

    oversized_login = Client().post('/api/v1/auth/token/', {
        'email': 'bounded@example.com',
        'password': 'x' * 257,
    }, content_type='application/json')
    assert oversized_login.status_code == 400
    assert 'password' in str(oversized_login.json())

    oversized_refresh = Client().post('/api/v1/auth/token/refresh/', {
        'refresh': 'x' * 4097,
    }, content_type='application/json')
    assert oversized_refresh.status_code == 400
    assert 'refresh' in str(oversized_refresh.json())

    decoded_uid_too_large = urlsafe_base64_encode(force_bytes('9' * 21))
    reset = PasswordResetConfirmSerializer(data={
        'uid': decoded_uid_too_large,
        'token': 'bounded-token',
        'new_password': NEW_PASSWORD,
    })
    assert not reset.is_valid()
    assert 'uid' in reset.errors


@pytest.mark.django_db
def test_registration_enforces_all_django_password_validators():
    weak_passwords = ['short', 'password1234', '123456789012345']
    for index, password in enumerate(weak_passwords):
        response = Client().post('/api/v1/auth/register/', {
            'name': f'Weak {index}',
            'slug': f'weak-{index}',
            'email': f'weak-{index}@example.com',
            'password': password,
        }, content_type='application/json')
        assert response.status_code == 400
        assert 'password' in str(response.json())


@pytest.mark.django_db
def test_change_password_validates_current_and_new_password_then_revokes_sessions():
    _, user, tokens = _human_session()
    client = Client(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    wrong = client.post('/api/v1/auth/change-password/', {
        'current_password': 'wrong-password',
        'new_password': NEW_PASSWORD,
    }, content_type='application/json')
    assert wrong.status_code == 400

    weak = client.post('/api/v1/auth/change-password/', {
        'current_password': PASSWORD,
        'new_password': 'short',
    }, content_type='application/json')
    assert weak.status_code == 400

    changed = client.post('/api/v1/auth/change-password/', {
        'current_password': PASSWORD,
        'new_password': NEW_PASSWORD,
    }, content_type='application/json')
    assert changed.status_code == 200
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert user.auth_version == 2
    assert client.get('/api/v1/auth/me/').status_code == 401
    assert Client().post('/api/v1/auth/token/refresh/', {
        'refresh': tokens['refresh'],
    }, content_type='application/json').status_code == 401


@pytest.mark.django_db
def test_security_endpoints_remain_available_in_billing_only_mode():
    tenant, _, tokens = _human_session(slug='past-due', email='past-due@example.com')
    Subscription.objects.filter(tenant=tenant).update(
        status=Subscription.STATUS_PAST_DUE,
        current_period_end=timezone.localdate(),
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    response = client.post('/api/v1/auth/change-password/', {
        'current_password': PASSWORD,
        'new_password': NEW_PASSWORD,
    }, content_type='application/json')
    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_password_reset_request_is_uniform_and_confirmation_is_one_time():
    _, user, tokens = _human_session(slug='reset', email='reset@example.com')
    with patch('apps.users.tasks.send_password_reset_email.delay') as enqueue:
        existing = Client().post('/api/v1/auth/password-reset/', {
            'email': user.email,
        }, content_type='application/json')
        existing_body = existing.json()
        assert existing.status_code == 202

        unknown = Client().post('/api/v1/auth/password-reset/', {
            'email': 'unknown@example.com',
        }, content_type='application/json')
        assert unknown.status_code == 202
        assert unknown.json() == existing_body
        assert enqueue.call_args_list[0].args == (user.pk,)
        assert enqueue.call_args_list[1].args == (None,)

    result = send_password_reset_email(user.pk)
    assert result == {'sent': True}
    assert len(mail.outbox) == 1

    reset_url = mail.outbox[0].body.splitlines()[2]
    parsed = urlparse(reset_url)
    assert parsed.query == ''
    params = parse_qs(parsed.fragment)
    payload = {
        'uid': params['uid'][0],
        'token': params['token'][0],
        'new_password': NEW_PASSWORD,
    }
    confirmed = Client().post(
        '/api/v1/auth/password-reset/confirm/',
        payload,
        content_type='application/json',
    )
    assert confirmed.status_code == 200
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert user.auth_version == 2

    replay = Client().post(
        '/api/v1/auth/password-reset/confirm/',
        payload,
        content_type='application/json',
    )
    assert replay.status_code == 400
    assert Client(
        HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
    ).get('/api/v1/auth/me/').status_code == 401


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_email_change_requires_password_and_confirmation_revokes_sessions():
    _, user, tokens = _human_session(slug='email-change', email='before@example.com')
    client = Client(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    missing = client.post('/api/v1/auth/change-email/', {
        'new_email': 'after@example.com',
    }, content_type='application/json')
    assert missing.status_code == 400
    assert len(mail.outbox) == 0

    wrong = client.post('/api/v1/auth/change-email/', {
        'new_email': 'after@example.com',
        'current_password': 'wrong-password',
    }, content_type='application/json')
    assert wrong.status_code == 400
    assert len(mail.outbox) == 0

    requested = client.post('/api/v1/auth/change-email/', {
        'new_email': 'after@example.com',
        'current_password': PASSWORD,
    }, content_type='application/json')
    assert requested.status_code == 200
    assert len(mail.outbox) == 1
    confirm_url = mail.outbox[0].body.splitlines()[2]
    token = parse_qs(urlparse(confirm_url).fragment)['token'][0]

    confirmed = Client().post(
        '/api/v1/auth/confirm-email/',
        {'token': token},
        content_type='application/json',
    )
    assert confirmed.status_code == 200
    user.refresh_from_db()
    assert user.email == 'after@example.com'
    assert user.auth_version == 2
    assert Client().post(
        '/api/v1/auth/confirm-email/',
        {'token': token},
        content_type='application/json',
    ).status_code == 400
    assert client.get('/api/v1/auth/me/').status_code == 401


@pytest.mark.django_db
def test_public_auth_throttles_are_configured_and_enforced(settings, monkeypatch):
    from apps.tenants.jwt_views import (
        BrowserLoginView,
        BrowserRefreshView,
        TenantTokenObtainPairView,
        TenantTokenRefreshView,
    )
    from apps.users.throttles import CredentialScopedRateThrottle
    from apps.users.views import PasswordResetConfirmView, PasswordResetRequestView

    rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
    assert rates['auth_login'] == '20/min'
    assert rates['auth_refresh'] == '60/min'
    assert rates['password_reset_request'] == '5/hour'
    assert rates['password_reset_confirm'] == '10/hour'
    assert TenantTokenObtainPairView.throttle_scope == 'auth_login'
    assert BrowserLoginView.throttle_scope == 'auth_login'
    assert TenantTokenRefreshView.throttle_scope == 'auth_refresh'
    assert BrowserRefreshView.throttle_scope == 'auth_refresh'
    assert PasswordResetRequestView.throttle_scope == 'password_reset_request'
    assert PasswordResetConfirmView.throttle_scope == 'password_reset_confirm'

    test_rates = {**rates, 'auth_login': '1/min'}
    monkeypatch.setattr(CredentialScopedRateThrottle, 'THROTTLE_RATES', test_rates)
    cache.clear()
    try:
        first = Client().post('/api/v1/auth/token/', {
            'email': 'throttled@example.com',
            'password': 'invalid-password',
        }, content_type='application/json', REMOTE_ADDR='203.0.113.10')
        second = Client().post('/api/v1/auth/token/', {
            'email': 'throttled@example.com',
            'password': 'invalid-password',
        }, content_type='application/json', REMOTE_ADDR='203.0.113.10')
        assert first.status_code == 401
        assert second.status_code == 429
    finally:
        cache.clear()


@pytest.mark.django_db
def test_logout_all_invalidates_tokens_for_every_membership():
    tenant, user, tokens = _human_session(slug='multi-session', email='member@example.com')
    second_tenant, _ = TenantService.create_tenant(
        'Second Owner',
        'other-owner',
        'other-owner@example.com',
        PASSWORD,
    )
    TenantUser.objects.create(user=user, tenant=second_tenant, role=TenantUser.ROLE_OPERATOR)
    membership = TenantUser.objects.get(user=user, tenant=second_tenant)
    user._current_tenant_membership = membership
    from apps.tenants.jwt_serializers import TenantTokenObtainPairSerializer
    second_refresh = TenantTokenObtainPairSerializer.get_token(user)

    response = Client(
        HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
    ).post('/api/v1/auth/logout-all/')
    assert response.status_code == 204
    assert Client().post('/api/v1/auth/token/refresh/', {
        'refresh': str(second_refresh),
    }, content_type='application/json').status_code == 401
    assert tenant.pk != second_tenant.pk
