from datetime import timedelta
from urllib.parse import unquote, urlparse

from celery import shared_task
from django.utils.timezone import now

from apps.datasources.models import DataSourceConnection
from apps.datasources.registry import get_adapter
from apps.products.services import (
    ProductBulkActionService, ProductEnrichmentService, ProductService,
)


def _write_sync_log(tenant, event_type: str, status: str, message: str) -> None:
    """Записывает событие в SyncLog — не падает при ошибках."""
    try:
        from apps.sync.models import SyncLog
        SyncLog.objects.create(
            tenant=tenant, event_type=event_type, status=status, message=message,
        )
    except Exception:
        pass


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

        _write_sync_log(
            tenant, 'datasource_import', 'ok',
            f'Импорт «{connection.name}»: создано {counts["created"]}, '
            f'обновлено {counts["updated"]}, без изменений {counts["unchanged"]}',
        )
        return counts

    except Exception as exc:
        connection.last_sync_status = DataSourceConnection.STATUS_ERROR
        connection.last_error = str(exc)
        connection.save(update_fields=['last_sync_status', 'last_error'])
        _write_sync_log(tenant, 'datasource_import', 'error', str(exc))
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing')
def parse_single_part(self, job_id: int):
    try:
        result = ProductEnrichmentService.run_parse_job(job_id)
        _queue_enrichment_images(result)
        return result
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing')
def parse_single_part_then_generate_description(self, job_id: int):
    try:
        result = ProductEnrichmentService.run_parse_job(job_id)
        _queue_enrichment_images(result)
        product_id = result.get('product_id')
        if product_id:
            from apps.ai_agent.tasks import generate_description_task
            generate_description_task.delay(product_id)
        return result
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing_bulk')
def process_bulk_product_action(self, bulk_job_id: int):
    try:
        return ProductBulkActionService.process_next_batch(bulk_job_id)
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='image_search')
def download_enrichment_images(
    self, product_id: int, image_urls: list[str], source_id: str = 'tachka',
):
    """Скачивает изображения, найденные parser enrichment, через текущий ProductImage pipeline."""
    from apps.products.models import Product, ProductImage
    from apps.products.storage import PhotoUploadPipeline

    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return {'error': f'Product {product_id} not found'}

    saved = 0
    pipeline = PhotoUploadPipeline()
    for url in _clean_enrichment_image_urls(image_urls)[:10]:
        image = pipeline.process(
            url,
            product,
            source_id=source_id,
            status=ProductImage.Status.NEEDS_REVIEW,
        )
        if image is not None:
            saved += 1
    return {'product_id': product_id, 'saved': saved}


def _clean_enrichment_image_urls(image_urls: list[str]) -> list[str]:
    """Фильтрует служебные картинки и дедуплицирует варианты одного product image."""
    result = []
    seen = set()
    for url in image_urls:
        identity = _enrichment_image_identity(url)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(url)
    return result


def _enrichment_image_identity(url: str) -> str:
    parsed = urlparse(str(url or '').strip())
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return ''
    path = unquote(parsed.path).lower()
    full = unquote(url).lower()
    if (
        parsed.netloc.endswith('getclicky.com')
        or 'brandlogos/' in path
        or path.endswith('.gif')
        or '/other/mask.' in full
    ):
        return ''
    if 'tachka.ru' in parsed.netloc and '/brand/' in path:
        return f'tachka:{path[path.index("/brand/"):]}'
    return f'{parsed.netloc}:{path}'


def _queue_enrichment_images(result: dict) -> None:
    product_id = result.get('product_id')
    image_urls = result.get('image_urls') or []
    if product_id and image_urls:
        download_enrichment_images.delay(
            product_id,
            image_urls,
            result.get('source_id') or 'tachka',
        )
