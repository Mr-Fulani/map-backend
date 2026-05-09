from datetime import timedelta

from celery import shared_task
from django.utils.timezone import now

from apps.datasources.models import DataSourceConnection
from apps.datasources.registry import get_adapter
from apps.products.services import ProductService


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='sync_import')
def import_from_datasource(self, connection_id: int):
    try:
        connection = DataSourceConnection.objects.select_related('tenant').get(pk=connection_id)
    except DataSourceConnection.DoesNotExist:
        return {'error': f'Connection {connection_id} not found'}

    tenant = connection.tenant
    adapter = get_adapter(connection)
    since = connection.last_sync_at or (now() - timedelta(days=30))

    counts = {'created': 0, 'updated': 0, 'unchanged': 0}
    offset = 0
    limit = 500

    try:
        while True:
            items = adapter.fetch_changes(since=since, limit=limit, offset=offset)
            if not items:
                break
            for item in items:
                _, status = ProductService.upsert_from_source(tenant, connection, item)
                counts[status] += 1
            offset += len(items)
            if len(items) < limit:
                break

        connection.last_sync_at = now()
        connection.last_sync_status = DataSourceConnection.STATUS_OK
        connection.last_error = ''
        connection.save(update_fields=['last_sync_at', 'last_sync_status', 'last_error'])
        return counts

    except Exception as exc:
        connection.last_sync_status = DataSourceConnection.STATUS_ERROR
        connection.last_error = str(exc)
        connection.save(update_fields=['last_sync_status', 'last_error'])
        raise self.retry(exc=exc)
