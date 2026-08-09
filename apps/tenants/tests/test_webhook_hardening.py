import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
)
from apps.tenants.models import (
    Tenant,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookEvent,
)
from apps.tenants.serializers import WebhookEndpointWriteSerializer
from apps.tenants.services import (
    DuplicateWebhookEndpoint,
    TenantService,
    WebhookEndpointQuotaExceeded,
    WebhookEndpointService,
)
from apps.tenants.tasks import (
    _finish_webhook_success,
    _start_webhook_delivery,
    dispatch_pending_webhooks,
    dispatch_webhook_event_task,
)
from apps.tenants.tests.auth import owner_client
from apps.tenants.views import WebhookEndpointListView, WebhookEndpointTestView
from apps.tenants.webhooks import enqueue_webhook_event


FERNET_KEY = 'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE='


@pytest.fixture(autouse=True)
def webhook_test_settings(settings, monkeypatch, request):
    settings.FIELD_ENCRYPTION_KEY = FERNET_KEY
    settings.FIELD_ENCRYPTION_KEYS = [FERNET_KEY]
    settings.WEBHOOK_ENDPOINTS_PER_TENANT = 20
    settings.WEBHOOK_DISPATCH_BATCH_SIZE = 100

    from apps.core import throttling

    cache = LocMemCache(f'webhook-hardening-{request.node.nodeid}', {})
    monkeypatch.setattr(throttling, 'coordination_cache', cache)
    monkeypatch.setattr(PrincipalScopedRateThrottle, 'cache', cache)
    monkeypatch.setattr(TenantScopedRateThrottle, 'cache', cache)
    return cache


def _endpoint(tenant, url, *, active=True):
    endpoint = WebhookEndpoint(
        tenant=tenant,
        url=url,
        events=['listing.published'],
        is_active=active,
    )
    endpoint.set_secret('endpoint-secret')
    endpoint.save()
    return endpoint


def _tenant_with_owner(slug):
    tenant, _ = TenantService.create_tenant(
        f'Webhook {slug}',
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant


def test_webhook_serializer_requires_https_without_dns_lookup():
    serializer = WebhookEndpointWriteSerializer(data={
        'url': 'http://8.8.8.8/hook',
        'events': ['listing.published'],
    })

    assert not serializer.is_valid()
    assert 'HTTPS' in str(serializer.errors['url'][0])


def test_webhook_serializer_canonicalizes_https_url():
    serializer = WebhookEndpointWriteSerializer(data={
        'url': 'https://8.8.8.8:443/hook#ignored',
        'events': ['listing.published'],
    })

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data['url'] == 'https://8.8.8.8/hook'


@pytest.mark.django_db
def test_live_endpoint_database_constraints_are_fail_closed():
    tenant = Tenant.objects.create(name='Webhook constraints', slug='webhook-constraints')
    _endpoint(tenant, 'https://8.8.8.8/hook', active=False)

    with pytest.raises(IntegrityError), transaction.atomic():
        _endpoint(tenant, 'https://8.8.8.8/hook')

    with pytest.raises(IntegrityError), transaction.atomic():
        _endpoint(tenant, 'http://8.8.8.8/insecure')


@pytest.mark.django_db
def test_inactive_endpoint_reserves_url_until_soft_delete():
    tenant = Tenant.objects.create(name='Webhook reuse', slug='webhook-reuse')
    endpoint, _ = WebhookEndpointService.create_endpoint(
        tenant=tenant,
        url='https://8.8.8.8/reusable',
        events=['listing.published'],
    )
    endpoint.is_active = False
    endpoint.save(update_fields=['is_active', 'updated_at'])

    with pytest.raises(DuplicateWebhookEndpoint):
        WebhookEndpointService.create_endpoint(
            tenant=tenant,
            url=endpoint.url,
            events=['listing.published'],
        )

    endpoint.soft_delete()
    replacement, _ = WebhookEndpointService.create_endpoint(
        tenant=tenant,
        url=endpoint.url,
        events=['listing.published'],
    )

    assert replacement.pk != endpoint.pk
    assert replacement.is_active is True


@pytest.mark.django_db
def test_endpoint_quota_counts_inactive_non_deleted_rows(settings):
    settings.WEBHOOK_ENDPOINTS_PER_TENANT = 2
    tenant = Tenant.objects.create(name='Webhook quota', slug='webhook-quota')
    first, _ = WebhookEndpointService.create_endpoint(
        tenant=tenant,
        url='https://8.8.8.8/one',
        events=['listing.published'],
    )
    first.is_active = False
    first.save(update_fields=['is_active', 'updated_at'])
    WebhookEndpointService.create_endpoint(
        tenant=tenant,
        url='https://8.8.8.8/two',
        events=['listing.published'],
    )

    with pytest.raises(WebhookEndpointQuotaExceeded):
        WebhookEndpointService.create_endpoint(
            tenant=tenant,
            url='https://8.8.8.8/three',
            events=['listing.published'],
        )


@pytest.mark.django_db
def test_create_api_returns_conflict_for_canonical_duplicate():
    tenant = _tenant_with_owner('webhook-api-duplicate')
    client = owner_client(tenant)
    payload = {
        'url': 'https://8.8.8.8:443/receiver#first',
        'events': ['listing.published'],
    }

    first = client.post('/api/v1/webhooks/', payload, content_type='application/json')
    duplicate = client.post(
        '/api/v1/webhooks/',
        {**payload, 'url': 'https://8.8.8.8/receiver'},
        content_type='application/json',
    )

    assert first.status_code == 201
    assert first.json()['data']['url'] == 'https://8.8.8.8/receiver'
    assert duplicate.status_code == 409
    assert duplicate.json()['code'] == 'duplicate_webhook_endpoint'


def test_webhook_mutations_have_principal_and_tenant_throttles(settings):
    rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
    assert rates['webhook_create_principal'] == '10/hour'
    assert rates['webhook_create_tenant'] == '20/hour'
    assert rates['webhook_test_principal'] == '6/min'
    assert rates['webhook_test_tenant'] == '20/min'
    assert WebhookEndpointListView.throttle_classes == [
        PrincipalScopedRateThrottle,
        TenantScopedRateThrottle,
    ]
    assert WebhookEndpointListView.principal_throttle_scope == 'webhook_create_principal'
    assert WebhookEndpointListView.tenant_throttle_scope == 'webhook_create_tenant'
    assert WebhookEndpointTestView.throttle_classes == [
        PrincipalScopedRateThrottle,
        TenantScopedRateThrottle,
    ]
    assert WebhookEndpointTestView.principal_throttle_scope == 'webhook_test_principal'
    assert WebhookEndpointTestView.tenant_throttle_scope == 'webhook_test_tenant'


@pytest.mark.django_db
def test_webhook_create_endpoint_is_rate_limited(monkeypatch):
    tenant = _tenant_with_owner('webhook-create-throttle')
    rates = {
        'webhook_create_principal': '1/min',
        'webhook_create_tenant': '1/min',
    }
    monkeypatch.setattr(PrincipalScopedRateThrottle, 'THROTTLE_RATES', rates)
    monkeypatch.setattr(TenantScopedRateThrottle, 'THROTTLE_RATES', rates)
    client = owner_client(tenant)

    first = client.post('/api/v1/webhooks/', {
        'url': 'https://8.8.8.8/first',
        'events': ['listing.published'],
    }, content_type='application/json')
    second = client.post('/api/v1/webhooks/', {
        'url': 'https://8.8.8.8/second',
        'events': ['listing.published'],
    }, content_type='application/json')

    assert first.status_code == 201
    assert second.status_code == 429


@pytest.mark.django_db
def test_webhook_test_endpoint_is_rate_limited(
    monkeypatch,
):
    tenant = _tenant_with_owner('webhook-test-throttle')
    endpoint = _endpoint(tenant, 'https://8.8.8.8/test-rate')
    rates = {
        'webhook_test_principal': '1/min',
        'webhook_test_tenant': '1/min',
    }
    monkeypatch.setattr(PrincipalScopedRateThrottle, 'THROTTLE_RATES', rates)
    monkeypatch.setattr(TenantScopedRateThrottle, 'THROTTLE_RATES', rates)
    client = owner_client(tenant)

    with patch(
        'apps.tenants.views.request_public_http_url',
        return_value=MagicMock(status_code=204),
    ):
        first = client.post(f'/api/v1/webhooks/{endpoint.pk}/test/')
        second = client.post(f'/api/v1/webhooks/{endpoint.pk}/test/')

    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.django_db
def test_inactive_webhook_cannot_be_used_for_test_delivery():
    tenant = _tenant_with_owner('webhook-inactive-test')
    endpoint = _endpoint(
        tenant,
        'https://8.8.8.8/inactive-test',
        active=False,
    )

    with patch('apps.tenants.views.request_public_http_url') as request:
        response = owner_client(tenant).post(
            f'/api/v1/webhooks/{endpoint.pk}/test/',
        )

    assert response.status_code == 404
    request.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_fanout_and_dispatchers_respect_configured_batch_limits(settings):
    settings.WEBHOOK_ENDPOINTS_PER_TENANT = 2
    settings.WEBHOOK_DISPATCH_BATCH_SIZE = 2
    tenant = Tenant.objects.create(name='Webhook fanout', slug='webhook-fanout')
    endpoints = [
        _endpoint(tenant, f'https://8.8.8.8/hook-{index}')
        for index in range(3)
    ]

    with patch('apps.tenants.webhooks._dispatch_event_safely'):
        capped_event = enqueue_webhook_event(
            tenant,
            'listing.published',
            {'listing_id': 1},
        )
    assert capped_event.deliveries.count() == 2

    event = WebhookEvent.objects.create(
        tenant=tenant,
        event_type='listing.published',
        payload={'listing_id': 2},
    )
    WebhookDelivery.objects.bulk_create([
        WebhookDelivery(event=event, endpoint=endpoint, endpoint_url=endpoint.url)
        for endpoint in endpoints
    ])

    with patch('apps.tenants.tasks.deliver_webhook_task.delay') as delay:
        first_result = dispatch_webhook_event_task(str(event.pk))
        second_result = dispatch_webhook_event_task(str(event.pk))
        exhausted_result = dispatch_webhook_event_task(str(event.pk))

        assert first_result == {'queued': 2, 'batch_limit': 2}
        assert second_result == {'queued': 1, 'batch_limit': 2}
        assert exhausted_result == {'queued': 0, 'batch_limit': 2}
        assert delay.call_count == 3
        assert all(len(call.args) == 2 for call in delay.call_args_list)
        assert event.deliveries.filter(
            status=WebhookDelivery.STATUS_QUEUED,
        ).count() == 3

    with patch('apps.tenants.tasks.deliver_webhook_task.delay') as delay:
        pending_result = dispatch_pending_webhooks()
        assert pending_result == {'queued': 2, 'batch_limit': 2}
        assert delay.call_count == 2


@pytest.mark.django_db(transaction=True)
def test_stale_webhook_worker_cannot_adopt_or_overwrite_new_claim():
    tenant = Tenant.objects.create(name='Webhook claims', slug='webhook-claims')
    endpoint = _endpoint(tenant, 'https://8.8.8.8/claims')
    event = WebhookEvent.objects.create(
        tenant=tenant,
        event_type='listing.published',
        payload={'listing_id': 3},
    )
    delivery = WebhookDelivery.objects.create(
        event=event,
        endpoint=endpoint,
        endpoint_url=endpoint.url,
    )

    old_claim, terminal_result = _start_webhook_delivery(delivery.pk)
    assert terminal_result is None
    assert old_claim is not None

    new_claim = uuid.uuid4()
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.STATUS_QUEUED,
        claim_token=new_claim,
        claimed_at=timezone.now(),
    )

    stale_start_claim, stale_start_result = _start_webhook_delivery(
        delivery.pk,
        old_claim,
    )
    assert stale_start_claim is None
    assert stale_start_result == {'status': 'stale_claim'}

    active_claim, active_result = _start_webhook_delivery(delivery.pk, new_claim)
    assert active_result is None
    assert active_claim == new_claim
    assert _finish_webhook_success(delivery.pk, old_claim, 202) == {
        'status': 'stale_claim',
    }

    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.STATUS_DELIVERING
    assert delivery.claim_token == new_claim
    assert delivery.response_status is None

    assert _finish_webhook_success(delivery.pk, new_claim, 204) == {
        'status': 'delivered',
        'http_status': 204,
    }
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.STATUS_DELIVERED
    assert delivery.claim_token is None


@pytest.mark.django_db(transaction=True)
def test_periodic_dispatch_recovers_stale_claim_with_a_new_token(settings):
    settings.WEBHOOK_DISPATCH_BATCH_SIZE = 1
    tenant = Tenant.objects.create(
        name='Webhook stale recovery',
        slug='webhook-stale-recovery',
    )
    endpoint = _endpoint(tenant, 'https://8.8.8.8/stale')
    event = WebhookEvent.objects.create(
        tenant=tenant,
        event_type='listing.published',
        payload={'listing_id': 4},
    )
    old_claim = uuid.uuid4()
    delivery = WebhookDelivery.objects.create(
        event=event,
        endpoint=endpoint,
        endpoint_url=endpoint.url,
        status=WebhookDelivery.STATUS_DELIVERING,
        claim_token=old_claim,
        claimed_at=timezone.now() - timedelta(minutes=16),
    )

    with patch('apps.tenants.tasks.deliver_webhook_task.delay') as delay:
        result = dispatch_pending_webhooks()

    assert result == {'queued': 1, 'batch_limit': 1}
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.STATUS_QUEUED
    assert delivery.claim_token is not None
    assert delivery.claim_token != old_claim
    delay.assert_called_once_with(delivery.pk, str(delivery.claim_token))
