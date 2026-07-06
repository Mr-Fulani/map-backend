from datetime import timedelta
import logging
from urllib.parse import unquote, urlparse

from celery import shared_task
from django.utils.timezone import now

from apps.datasources.models import DataSourceConnection
from apps.datasources.registry import get_adapter
from apps.products.models import Product
from apps.products.services import (
    ProductBulkActionService, ProductEnrichmentService, ProductService,
)

logger = logging.getLogger(__name__)


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
                product, status, change_type = ProductService.upsert_from_source(
                    tenant, connection, item,
                )
                counts[status] += 1
                if status == 'updated' and change_type:
                    sync_product_listings_task.delay(product.pk, change_type)
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
        # Авто-классификация: после импорта классифицируем новые/изменённые товары,
        # чтобы не требовать ручного запуска (категория нужна для маппинга на Avito).
        if counts['created'] or counts['updated']:
            classify_tenant_products.delay(tenant.id)
        return counts

    except Exception as exc:
        connection.last_sync_status = DataSourceConnection.STATUS_ERROR
        connection.last_error = str(exc)
        connection.save(update_fields=['last_sync_status', 'last_error'])
        _write_sync_log(tenant, 'datasource_import', 'error', str(exc))
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='sync_import')
def classify_tenant_products(self, tenant_id: int):
    """
    Классифицирует домен и категорию каталога у ещё не классифицированных товаров тенанта.

    Запускается автоматически после импорта из источника. Берёт только товары без
    ProductCatalogClassification (новые/никогда не прогонявшиеся) — ручные привязки
    (source=MANUAL) classify_product_catalog_domain не перетирает. Идемпотентна:
    повторный запуск трогает только незаполненное.
    """
    qs = (
        Product.objects
        .filter(tenant_id=tenant_id, catalog_classification__isnull=True)
        .select_related('catalog_category')
    )
    classified = 0
    for product in qs.iterator():
        try:
            ProductEnrichmentService.classify_product_catalog_domain(product)
            classified += 1
        except Exception:
            logger.exception('Не удалось классифицировать product=%s', product.pk)
    return {'classified': classified}


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='sync_import')
def reclassify_products_for_categories(self, tenant_id: int, category_ids: list[int]):
    """
    Переопределяет категорию товаров, привязанных к отключённым категориям каталога.

    Запускается при отключении ветки категорий: товары не должны оставаться
    в выключенной категории. Сбрасывает catalog_category и заново прогоняет
    маппинг источника + авто-классификацию. Ручные классификации
    (source=MANUAL) не трогает.
    """
    from apps.products.models import ProductCatalogClassification

    qs = (
        Product.objects
        .filter(tenant_id=tenant_id, catalog_category_id__in=category_ids)
        .exclude(catalog_classification__source=ProductCatalogClassification.Source.MANUAL)
    )
    reclassified = 0
    for product in qs.iterator():
        try:
            product.catalog_category = None
            product.save(update_fields=['catalog_category', 'updated_at'])
            ProductEnrichmentService.get_product_tenant_category(product)
            ProductEnrichmentService.classify_product_catalog_domain(product)
            reclassified += 1
        except Exception:
            logger.exception('Не удалось переклассифицировать product=%s', product.pk)
    return {'reclassified': reclassified}


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='sync_import')
def sync_product_listings_task(self, product_id: int, change_type: str):
    """
    Распространяет изменение товара из источника данных на активные листинги.

    Стратегия по типу изменения:
    - price_only  → обновить price_on_listing во всех активных листингах, вызвать
                    update_price_task (REST, без фида), уведомить админа в Telegram.
    - stock_only  → если stock_qty == 0: снять с публикации все активные листинги
                    и уведомить; если > 0: уведомить о возврате товара в наличие.
    - content     → обновить фид через update_listing_task для каждого листинга.
    - category    → то же, что content.
    """
    from apps.marketplaces.models import Listing
    from apps.marketplaces.tasks import update_listing_task, update_price_task, unpublish_listing_task
    from apps.notifications.services import LEVEL_ERROR, LEVEL_SUCCESS
    from apps.notifications.tasks import send_notification_task

    try:
        product = Product.objects.select_related('tenant').get(pk=product_id)
    except Product.DoesNotExist:
        return

    tenant = product.tenant
    listings = list(
        Listing.objects.filter(
            product=product,
            tenant=tenant,
            status=Listing.STATUS_ACTIVE,
        ).select_related('account')
    )

    if change_type == 'price_only':
        for listing in listings:
            listing.price_on_listing = product.price
            listing.save(update_fields=['price_on_listing'])
            update_price_task.delay(listing.pk)
        if listings:
            msg = (
                f'Цена изменена: «{product.name}» ({product.brand}) → {product.price} ₽. '
                f'Листингов обновлено: {len(listings)}.'
            )
            send_notification_task.delay(tenant.pk, LEVEL_ERROR, msg)

    elif change_type == 'stock_only':
        if product.stock_qty == 0:
            for listing in listings:
                listing.status = Listing.STATUS_ARCHIVED
                listing.save(update_fields=['status'])
                unpublish_listing_task.delay(listing.pk)
            if listings:
                msg = (
                    f'Товар закончился: «{product.name}» ({product.brand}) — 0 шт. '
                    f'Снято листингов: {len(listings)}.'
                )
                send_notification_task.delay(tenant.pk, LEVEL_ERROR, msg)
        else:
            msg = (
                f'Товар вернулся в наличие: «{product.name}» ({product.brand}) — '
                f'{product.stock_qty} шт. При необходимости создайте листинг.'
            )
            send_notification_task.delay(tenant.pk, LEVEL_SUCCESS, msg)

    elif change_type in ('content', 'category'):
        for listing in listings:
            listing.price_on_listing = product.price
            listing.save(update_fields=['price_on_listing'])
            update_listing_task.delay(listing.pk)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing')
def parse_single_part(self, job_id: int):
    try:
        result = ProductEnrichmentService.run_parse_job(job_id)
        _save_enrichment_images(result)
        return result
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing')
def parse_single_part_then_generate_description(self, job_id: int):
    try:
        result = ProductEnrichmentService.run_parse_job(job_id)
        _save_enrichment_images(result)
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
    from apps.products.models import Product

    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return {'error': f'Product {product_id} not found'}

    return _download_enrichment_images(product, image_urls, source_id)


def _download_enrichment_images(product, image_urls: list[str], source_id: str = 'tachka') -> dict:
    from apps.products.models import ProductImage
    from apps.products.storage import PhotoUploadPipeline

    saved = 0
    pipeline = PhotoUploadPipeline()
    for url in _clean_enrichment_image_urls(image_urls):
        image = pipeline.process(
            url,
            product,
            source_id=source_id,
            status=ProductImage.Status.NEEDS_REVIEW,
        )
        if image is not None:
            saved += 1
    return {'product_id': product.pk, 'saved': saved}


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
        or 'placeholder' in path
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


def _save_enrichment_images(result: dict) -> None:
    product_id = result.get('product_id')
    image_urls = result.get('image_urls') or []
    if not product_id or not image_urls:
        return

    try:
        from apps.products.models import Product
        product = Product.objects.get(pk=product_id)
        _download_enrichment_images(product, image_urls, result.get('source_id') or 'tachka')
    except Exception:
        logger.warning(
            'Failed to save enrichment images for product=%s',
            product_id,
            exc_info=True,
        )
