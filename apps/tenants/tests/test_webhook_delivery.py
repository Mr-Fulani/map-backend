import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.tenants.models import (
    Tenant, WebhookDelivery, WebhookEndpoint, WebhookEvent,
)
from apps.tenants.tasks import deliver_webhook_task
from apps.tenants.webhooks import enqueue_webhook_event


FERNET_KEY = 'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE='


def make_endpoint(tenant, *, url='https://hooks.example.com/map'):
    endpoint = WebhookEndpoint(
        tenant=tenant,
        url=url,
        events=['listing.published'],
    )
    endpoint.set_secret('top-secret-signing-key')
    endpoint.save()
    return endpoint


@pytest.mark.django_db(transaction=True)
class TestWebhookOutbox:
    @pytest.fixture(autouse=True)
    def encryption_settings(self, settings):
        settings.FIELD_ENCRYPTION_KEY = FERNET_KEY
        settings.FIELD_ENCRYPTION_KEYS = [FERNET_KEY]

    def test_secret_is_encrypted_and_outbox_is_created(self):
        tenant = Tenant.objects.create(name='Hooks', slug='hooks')
        endpoint = make_endpoint(tenant)

        with patch('apps.tenants.webhooks._dispatch_event_safely'):
            event = enqueue_webhook_event(
                tenant,
                'listing.published',
                {'listing_id': 42},
            )

        endpoint.refresh_from_db()
        assert b'top-secret-signing-key' not in bytes(endpoint.secret_encrypted)
        assert endpoint.get_secret() == 'top-secret-signing-key'
        assert WebhookEvent.objects.filter(pk=event.pk).exists()
        assert WebhookDelivery.objects.filter(event=event, endpoint=endpoint).exists()

    def test_successful_delivery_is_signed_and_marked_delivered(self):
        tenant = Tenant.objects.create(name='Delivery', slug='delivery')
        make_endpoint(tenant)
        with patch('apps.tenants.webhooks._dispatch_event_safely'):
            event = enqueue_webhook_event(
                tenant,
                'listing.published',
                {'listing_id': 7},
            )
        delivery = event.deliveries.get()
        response = MagicMock(status_code=204, headers={}, encoding='utf-8')

        with patch(
            'apps.tenants.tasks.request_public_http_url',
            return_value=response,
        ) as post:
            result = deliver_webhook_task(delivery.pk)

        delivery.refresh_from_db()
        assert result['status'] == 'delivered'
        assert delivery.status == WebhookDelivery.STATUS_DELIVERED
        assert post.call_args.kwargs['method'] == 'POST'
        assert post.call_args.kwargs['status_only'] is True
        assert post.call_args.kwargs['redirect_policy'] == 'none'
        assert post.call_args.kwargs['headers']['X-MAP-Signature'].startswith('sha256=')

    def test_success_status_does_not_depend_on_response_body(self):
        tenant = Tenant.objects.create(name='Delivery body', slug='delivery-body')
        make_endpoint(tenant)
        with patch('apps.tenants.webhooks._dispatch_event_safely'):
            event = enqueue_webhook_event(
                tenant,
                'listing.published',
                {'listing_id': 9},
            )
        delivery = event.deliveries.get()
        response = MagicMock(status_code=204, headers={}, encoding='utf-8')

        with patch(
            'apps.tenants.tasks.request_public_http_url',
            return_value=response,
        ) as request:
            result = deliver_webhook_task(delivery.pk)

        delivery.refresh_from_db()
        assert result['status'] == 'delivered'
        assert delivery.status == WebhookDelivery.STATUS_DELIVERED
        assert delivery.response_body == ''
        assert request.call_args.kwargs['status_only'] is True

    def test_failed_delivery_is_persisted_as_safe_retry_error(self, caplog):
        tenant = Tenant.objects.create(name='Retry', slug='retry')
        secret_query = 'third-party-token-super-secret'
        make_endpoint(
            tenant,
            url=f'https://hooks.example.com/map?token={secret_query}',
        )
        with patch('apps.tenants.webhooks._dispatch_event_safely'):
            event = enqueue_webhook_event(
                tenant,
                'listing.published',
                {'listing_id': 8},
            )
        delivery = event.deliveries.get()

        caplog.set_level(logging.WARNING, logger='apps.tenants.tasks')
        with patch(
            'apps.tenants.tasks.request_public_http_url',
            side_effect=requests.ConnectionError(
                f'connection failed for https://hooks.example.com/map?token={secret_query}',
            ),
        ):
            deliver_webhook_task(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == WebhookDelivery.STATUS_RETRY
        assert delivery.attempts == 1
        assert delivery.next_attempt_at is not None
        assert delivery.last_error.startswith('transport_error:')
        assert secret_query not in delivery.last_error
        assert secret_query not in caplog.text
        assert '?token=' not in delivery.last_error
        assert '?token=' not in caplog.text


@pytest.mark.django_db
class TestSoftDelete:
    def test_datasource_is_hidden_but_recoverable(self):
        from apps.datasources.models import DataSourceConnection

        tenant = Tenant.objects.create(name='Retention', slug='retention')
        connection = DataSourceConnection.objects.create(
            tenant=tenant,
            name='1C',
            type=DataSourceConnection.TYPE_1C_HTTP,
            credentials=b'encrypted-placeholder',
        )

        connection.delete()

        assert not DataSourceConnection.objects.filter(pk=connection.pk).exists()
        deleted = DataSourceConnection.all_objects.get(pk=connection.pk)
        assert deleted.deleted_at is not None
        assert deleted.is_active is False
        deleted.restore()
        assert DataSourceConnection.objects.filter(pk=connection.pk).exists()

    def test_retention_purge_physically_removes_expired_soft_delete(self, settings):
        from apps.core.retention import purge_retained_data
        from apps.datasources.models import DataSourceConnection

        settings.SOFT_DELETE_RETENTION_DAYS = 0
        settings.WEBHOOK_AUDIT_RETENTION_DAYS = 180
        settings.BILLING_AUDIT_RETENTION_DAYS = 730
        settings.SYNC_LOG_RETENTION_DAYS = 90
        tenant = Tenant.objects.create(name='Purge', slug='purge')
        connection = DataSourceConnection.objects.create(
            tenant=tenant,
            name='Expired',
            type=DataSourceConnection.TYPE_CSV,
            credentials=b'encrypted-placeholder',
        )
        connection.delete()

        preview = purge_retained_data(dry_run=True)
        assert preview['datasource_connections'] == 1
        assert DataSourceConnection.all_objects.filter(pk=connection.pk).exists()

        purge_retained_data()
        assert not DataSourceConnection.all_objects.filter(pk=connection.pk).exists()
