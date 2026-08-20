from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from celery.app.task import Context
from celery.exceptions import MaxRetriesExceededError, Retry
from django.test import Client

from apps.datasources.models import DataSourceConnection
from apps.products.tasks import import_from_datasource
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import create_operator_key, owner_access_token


@pytest.mark.django_db
def test_datasource_import_is_skipped_after_subscription_expires():
    tenant, _ = TenantService.create_tenant(
        'Expired Import', 'expired-import-co', 'expired-import@test.com', 'pass12345',
    )
    sub = tenant.subscription
    sub.current_period_end = date.today() - timedelta(days=1)
    sub.save(update_fields=['current_period_end'])
    connection = DataSourceConnection.objects.create(
        tenant=tenant,
        name='1C',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=b'opaque',
    )

    with patch('apps.products.tasks.get_adapter') as get_adapter:
        result = import_from_datasource(connection.pk)

    connection.refresh_from_db()
    assert result['skipped'] is True
    assert 'неактивна' in result['reason']
    assert connection.last_sync_status == DataSourceConnection.STATUS_ERROR
    assert get_adapter.called is False


@pytest.mark.django_db
def test_inactive_datasource_is_rejected_at_api_worker_and_beat_boundaries():
    tenant, _ = TenantService.create_tenant(
        'Inactive Source',
        'inactive-source',
        'inactive-source@test.com',
        'pass12345',
    )
    inactive = DataSourceConnection.objects.create(
        tenant=tenant,
        name='Disabled 1C',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=b'opaque',
        is_active=False,
    )
    active = DataSourceConnection.objects.create(
        tenant=tenant,
        name='Active 1C',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=b'opaque',
        is_active=True,
    )
    machine_key = create_operator_key(tenant)

    with patch('apps.products.views.import_from_datasource.delay') as product_delay:
        machine_response = Client(
            HTTP_AUTHORIZATION=f'Bearer {machine_key}',
        ).post(f'/api/v1/products/sync/{inactive.pk}/')
    with patch('apps.products.tasks.get_adapter') as get_adapter:
        worker_result = import_from_datasource(inactive.pk)
    with patch('apps.products.tasks.import_from_datasource.delay') as human_delay:
        human_response = Client(
            HTTP_AUTHORIZATION=f'Bearer {owner_access_token(tenant)}',
        ).post(f'/api/v1/datasources/{inactive.pk}/sync/')
    with patch('apps.products.tasks.import_from_datasource.delay') as beat_delay:
        from apps.sync.tasks import sync_all_tenants
        beat_result = sync_all_tenants()

    assert machine_response.status_code == 404
    product_delay.assert_not_called()
    assert worker_result == {
        'skipped': True,
        'reason': 'connection_not_found_or_inactive',
    }
    get_adapter.assert_not_called()
    assert human_response.status_code == 404
    human_delay.assert_not_called()
    beat_delay.assert_called_once_with(active.pk)
    assert beat_result == {'connections_queued': 1}


@pytest.mark.django_db
def test_datasource_import_metrics_distinguish_retry_from_exhausted_failure():
    tenant, _ = TenantService.create_tenant(
        'Import Retry Metrics',
        'import-retry-metrics',
        'import-retry-metrics@test.com',
        'pass12345',
    )
    connection = DataSourceConnection.objects.create(
        tenant=tenant,
        name='Failing 1C',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=b'opaque',
    )
    adapter = SimpleNamespace(
        fetch_changes=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError('source unavailable'),
        ),
    )

    with (
        patch('apps.products.tasks.get_adapter', return_value=adapter),
        patch('apps.products.tasks.metric_count') as count,
        patch('apps.products.tasks.metric_distribution'),
        patch.object(import_from_datasource, 'retry', side_effect=Retry()),
        pytest.raises(Retry),
    ):
        import_from_datasource(connection.pk)

    assert count.call_args.kwargs['attributes']['outcome'] == 'retry'

    import_from_datasource.request_stack.push(
        Context(retries=import_from_datasource.max_retries),
    )
    try:
        with (
            patch('apps.products.tasks.get_adapter', return_value=adapter),
            patch('apps.products.tasks.metric_count') as count,
            patch('apps.products.tasks.metric_distribution'),
            patch.object(
                import_from_datasource,
                'retry',
                side_effect=MaxRetriesExceededError(),
            ),
            pytest.raises(MaxRetriesExceededError),
        ):
            import_from_datasource(connection.pk)
    finally:
        import_from_datasource.request_stack.pop()

    assert count.call_args.kwargs['attributes']['outcome'] == 'failure'
