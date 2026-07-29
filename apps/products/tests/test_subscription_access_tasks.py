from datetime import date, timedelta
from unittest.mock import patch

import pytest

from apps.datasources.models import DataSourceConnection
from apps.products.tasks import import_from_datasource
from apps.tenants.services import TenantService


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
