import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue='sync_import')
def sync_all_tenants():
    """
    Запускает импорт данных для всех активных тенантов с включёнными источниками.

    Запускается каждые 5 минут через Celery Beat.
    """
    from apps.datasources.models import DataSourceConnection
    from apps.products.tasks import import_from_datasource

    connections = DataSourceConnection.objects.filter(
        tenant__is_active=True,
        is_active=True,
    ).values_list('pk', flat=True)

    for conn_id in connections:
        import_from_datasource.delay(conn_id)

    logger.info('sync_all_tenants: запущено %d заданий импорта', len(connections))
    return {'connections_queued': len(connections)}
