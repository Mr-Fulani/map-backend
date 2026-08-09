from unittest.mock import MagicMock, patch

import pytest

from apps.core.url_security import UnsafePublicURL
from apps.tenants.models import WebhookEndpoint
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_client


FERNET_KEY = 'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE='


def _endpoint(tenant):
    endpoint = WebhookEndpoint(
        tenant=tenant,
        url='https://hooks.example.com/map',
        events=['listing.published'],
    )
    endpoint.set_secret('test-secret')
    endpoint.save()
    return endpoint


@pytest.mark.django_db
def test_webhook_test_endpoint_uses_status_only_pinned_transport(settings):
    settings.FIELD_ENCRYPTION_KEY = FERNET_KEY
    settings.FIELD_ENCRYPTION_KEYS = [FERNET_KEY]
    tenant, _ = TenantService.create_tenant(
        'Webhook test', 'webhook-test-status', 'hook-status@test.com', 'pass12345',
    )
    endpoint = _endpoint(tenant)
    remote_response = MagicMock(status_code=204)

    with patch(
        'apps.tenants.views.request_public_http_url',
        return_value=remote_response,
    ) as request:
        response = owner_client(tenant).post(f'/api/v1/webhooks/{endpoint.pk}/test/')

    assert response.status_code == 200
    assert response.json()['data'] == {'http_status': 204, 'ok': True}
    assert request.call_args.kwargs['method'] == 'POST'
    assert request.call_args.kwargs['status_only'] is True
    assert request.call_args.kwargs['redirect_policy'] == 'none'


@pytest.mark.django_db
def test_webhook_test_endpoint_rejects_unsafe_destination(settings):
    settings.FIELD_ENCRYPTION_KEY = FERNET_KEY
    settings.FIELD_ENCRYPTION_KEYS = [FERNET_KEY]
    tenant, _ = TenantService.create_tenant(
        'Webhook unsafe', 'webhook-test-unsafe', 'hook-unsafe@test.com', 'pass12345',
    )
    endpoint = _endpoint(tenant)

    with patch(
        'apps.tenants.views.request_public_http_url',
        side_effect=UnsafePublicURL('private destination'),
    ):
        response = owner_client(tenant).post(f'/api/v1/webhooks/{endpoint.pk}/test/')

    assert response.status_code == 400
    assert 'private destination' in response.json()['detail']
