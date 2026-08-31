from datetime import datetime, timedelta
import logging
import time
from typing import TypedDict
from urllib.parse import unquote, urlparse

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils.timezone import now

from apps.core.telemetry import metric_count, metric_distribution
from apps.datasources.models import DataSourceConnection
from apps.datasources.registry import get_adapter
from apps.products.models import Product, ProductBulkActionJob, ProductParseJob
from apps.products.services import (
    ProductBulkActionService, ProductEnrichmentService, ProductService,
)

logger = logging.getLogger(__name__)


class _ProductListingExpectedState(TypedDict):
    expected_status: str
    expected_account_id: int
    expected_external_id: str | None
    expected_deleted_at: datetime | None
    expected_product_updated_at: datetime


def _product_listing_expected_state(
    listing,
    product,
) -> _ProductListingExpectedState:
    return {
        'expected_status': listing.status,
        'expected_account_id': listing.account_id,
        'expected_external_id': listing.external_id,
        'expected_deleted_at': listing.deleted_at,
        'expected_product_updated_at': product.updated_at,
    }


def _save_product_listing_intent(
    listing,
    update_fields,
    *,
    expected_state: _ProductListingExpectedState,
) -> bool:
    """Compare-and-apply a product-driven intent through the shared fence."""

    from apps.marketplaces.services import _save_local_listing_intent

    return _save_local_listing_intent(
        listing,
        update_fields,
        expected_status=expected_state['expected_status'],
        expected_account_id=expected_state['expected_account_id'],
        expected_external_id=expected_state['expected_external_id'],
        expected_deleted_at=expected_state['expected_deleted_at'],
        expected_product_updated_at=expected_state['expected_product_updated_at'],
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


def _schedule_ozon_autofill(product_id: int, trigger_key: str) -> None:
    """Ozon preparation must never turn successful enrichment into a failure."""
    try:
        from apps.marketplaces.ozon_autofill import schedule_ozon_autofill

        schedule_ozon_autofill(product_id, trigger_key=trigger_key)
    except Exception:
        logger.exception('Не удалось поставить Ozon autofill product=%s', product_id)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='sync_import')
def import_from_datasource(self, connection_id: int):
    started_at = time.monotonic()
    try:
        connection = DataSourceConnection.objects.select_related('tenant').get(
            pk=connection_id,
            is_active=True,
            tenant__is_active=True,
        )
    except DataSourceConnection.DoesNotExist:
        metric_count(
            'map.sync.attempt',
            attributes={'source_type': 'other', 'outcome': 'skipped'},
        )
        return {
            'skipped': True,
            'reason': 'connection_not_found_or_inactive',
        }

    # Uploaded CSV/Excel files are processed synchronously by CSVUploadView.
    # Keep this worker-side guard as a boundary for already queued tasks and
    # direct callers; a file connection has no remote source to poll.
    if connection.type == DataSourceConnection.TYPE_CSV:
        metric_count(
            'map.sync.attempt',
            attributes={'source_type': connection.type, 'outcome': 'skipped'},
        )
        return {
            'skipped': True,
            'reason': 'file_upload_source_not_pollable',
        }

    tenant = connection.tenant
    from apps.billing.services import LimitChecker
    can_import, reason = LimitChecker().can_import_sku(tenant, count=0)
    if not can_import:
        connection.last_sync_status = DataSourceConnection.STATUS_ERROR
        connection.last_error = reason
        connection.save(update_fields=['last_sync_status', 'last_error'])
        _write_sync_log(tenant, 'datasource_import', 'warn', reason)
        metric_count(
            'map.sync.attempt',
            attributes={'source_type': connection.type, 'outcome': 'skipped'},
        )
        return {'skipped': True, 'reason': reason}

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
            # A source page is one domain transaction. The service acquires
            # account/endpoint locks before product locks and advances each
            # affected account cursor at most once for the entire page.
            page_results = ProductService.upsert_batch_from_source(
                tenant,
                connection,
                items,
            )
            for product, status, change_type in page_results:
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
        metric_count(
            'map.sync.attempt',
            attributes={'source_type': connection.type, 'outcome': 'success'},
        )
        metric_distribution(
            'map.sync.attempt.duration',
            time.monotonic() - started_at,
            unit='second',
            attributes={'source_type': connection.type, 'outcome': 'success'},
        )
        for item_result, count in counts.items():
            metric_count(
                'map.sync.items',
                count,
                attributes={'source_type': connection.type, 'result': item_result},
            )
        return counts

    except Exception as exc:
        connection.last_sync_status = DataSourceConnection.STATUS_ERROR
        connection.last_error = str(exc)
        connection.save(update_fields=['last_sync_status', 'last_error'])
        _write_sync_log(tenant, 'datasource_import', 'error', str(exc))
        will_retry = self.request.retries < self.max_retries
        outcome = 'retry' if will_retry else 'failure'
        metric_count(
            'map.sync.attempt',
            attributes={'source_type': connection.type, 'outcome': outcome},
        )
        metric_distribution(
            'map.sync.attempt.duration',
            time.monotonic() - started_at,
            unit='second',
            attributes={'source_type': connection.type, 'outcome': outcome},
        )
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
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            capture_product_feed_generations,
            locked_product_feed_write,
        )

        for attempt in range(3):
            generation = capture_product_feed_generations((product.pk,)).get(product.pk)
            if generation is None:
                break
            try:
                with locked_product_feed_write((generation,)) as locked:
                    current = locked[product.pk]
                    current.catalog_category = None
                    current.save(update_fields=['catalog_category', 'updated_at'])
                    ProductEnrichmentService._classify_product_catalog_domain_locked(
                        current,
                    )
                    reclassified += 1
                break
            except StaleProductFeedWrite:
                if attempt == 2:
                    logger.warning(
                        'Товар менялся во время переклассификации product=%s',
                        product.pk,
                    )
            except Exception:
                logger.exception('Не удалось переклассифицировать product=%s', product.pk)
                break
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
        applied_count = 0
        for listing in listings:
            expected_state = _product_listing_expected_state(listing, product)
            listing.price_on_listing = product.price
            applied = _save_product_listing_intent(
                listing,
                ('price_on_listing',),
                expected_state=expected_state,
            )
            if applied:
                update_price_task.delay(listing.pk)
                applied_count += 1
        if applied_count:
            msg = (
                f'Цена изменена: «{product.name}» ({product.brand}) → {product.price} ₽. '
                f'Листингов обновлено: {applied_count}.'
            )
            send_notification_task.delay(tenant.pk, LEVEL_ERROR, msg)

    elif change_type == 'stock_only':
        if product.stock_qty == 0:
            applied_count = 0
            for listing in listings:
                # This records a local removal intent. Only the provider
                # result may later confirm the terminal archived state.
                expected_state = _product_listing_expected_state(listing, product)
                listing.status = Listing.STATUS_ARCHIVING
                applied = _save_product_listing_intent(
                    listing,
                    ('status',),
                    expected_state=expected_state,
                )
                if applied:
                    unpublish_listing_task.delay(listing.pk)
                    applied_count += 1
            if applied_count:
                msg = (
                    f'Товар закончился: «{product.name}» ({product.brand}) — 0 шт. '
                    f'Снято листингов: {applied_count}.'
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
            expected_state = _product_listing_expected_state(listing, product)
            listing.price_on_listing = product.price
            applied = _save_product_listing_intent(
                listing,
                ('price_on_listing',),
                expected_state=expected_state,
            )
            if applied:
                update_listing_task.delay(listing.pk)


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing')
def parse_single_part(self, job_id: int):
    result = ProductEnrichmentService.run_parse_job(job_id)
    _save_enrichment_images(result)
    product_id = result.get('product_id')
    if product_id:
        from apps.core.dispatch import enqueue_durable_task
        job = ProductParseJob.objects.only('fallback_origin_key').get(pk=job_id)
        origin_key = job.fallback_origin_key or f'parse-job:{job_id}'
        _schedule_ozon_autofill(product_id, f'parse-job:{job_id}')
        enqueue_durable_task(
            'apps.web_research.tasks.schedule_web_research_fallback',
            args=[product_id, False, origin_key],
            deduplication_key=f'{origin_key}:web-research-fallback',
            max_run_attempts=13,
        )
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='part_parsing')
def parse_single_part_then_generate_description(self, job_id: int):
    result = ProductEnrichmentService.run_parse_job(job_id)
    _save_enrichment_images(result)
    product_id = result.get('product_id')
    if product_id:
        from apps.core.dispatch import enqueue_durable_task
        job = ProductParseJob.objects.only('fallback_origin_key').get(pk=job_id)
        origin_key = job.fallback_origin_key or f'parse-job:{job_id}'
        _schedule_ozon_autofill(product_id, f'parse-job:{job_id}')
        enqueue_durable_task(
            'apps.web_research.tasks.schedule_web_research_fallback',
            args=[product_id, True, origin_key],
            # Keep the upgrade-to-generate signal distinct from sibling
            # non-generating fallbacks. Both carry the same origin_key, so
            # WebResearchRun coalesces the paid search while preserving the
            # stronger generate_after intent.
            deduplication_key=(
                f'{origin_key}:web-research-fallback:generate-after'
            ),
            max_run_attempts=13,
        )
    return result


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    queue='part_parsing_bulk',
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_bulk_product_action(self, bulk_job_id: int):
    return ProductBulkActionService.process_next_batch(bulk_job_id)


@shared_task(
    queue='part_parsing_bulk',
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def dispatch_due_product_bulk_jobs(limit: int = 100):
    """Reconcile PENDING/due bulk jobs into the durable dispatch outbox."""
    dispatch_time = now()
    batch_limit = max(1, min(int(limit), 500))

    due_state = (
        Q(status=ProductBulkActionJob.Status.PENDING)
        | Q(
            status=ProductBulkActionJob.Status.COOLING_DOWN,
            next_batch_at__lte=dispatch_time,
        )
    )
    with transaction.atomic():
        jobs = list(
            ProductBulkActionJob.objects
            .select_for_update(skip_locked=True)
            .filter(due_state)
            .order_by('next_batch_at', 'created_at')
            [:batch_limit]
        )
        ProductBulkActionJob.objects.filter(pk__in=[job.pk for job in jobs]).update(
            last_dispatched_at=dispatch_time,
        )
        from apps.core.dispatch import enqueue_durable_task
        for job in jobs:
            enqueue_durable_task(
                'apps.products.tasks.process_bulk_product_action',
                args=[job.pk],
                deduplication_key=(
                    f'product-bulk:{job.pk}:batch-offset:{job.processed_count}'
                ),
                available_at=dispatch_time,
                max_run_attempts=4,
            )

    return {'selected': len(jobs)}


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
            validate_quality=True,
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


def _save_enrichment_images(result: dict) -> dict:
    product_id = result.get('product_id')
    image_urls = _clean_enrichment_image_urls(result.get('image_urls') or [])
    summary = {
        'state': 'completed',
        'found_count': len(image_urls),
        'saved_count': 0,
        'error': '',
    }
    if not product_id or not image_urls:
        _record_enrichment_image_result(result, summary)
        return summary

    try:
        from apps.products.models import Product
        product = Product.objects.get(pk=product_id)
        download_result = _download_enrichment_images(
            product,
            image_urls,
            result.get('source_id') or 'tachka',
        )
        summary['saved_count'] = download_result['saved']
    except Exception as exc:
        summary['error'] = str(exc)[:500]
        logger.warning(
            'Failed to save enrichment images for product=%s',
            product_id,
            exc_info=True,
        )
    _record_enrichment_image_result(result, summary)
    return summary


def _record_enrichment_image_result(result: dict, summary: dict) -> None:
    """Помечает обработку фото завершённой после фактического сохранения файлов."""
    job_id = result.get('job_id')
    if not job_id:
        return
    from apps.products.models import ProductParseJob
    job = ProductParseJob.objects.filter(pk=job_id).first()
    if job is None:
        return
    parsed_data = dict(job.parsed_data or {})
    parsed_data['image_processing'] = summary
    job.parsed_data = parsed_data
    job.save(update_fields=['parsed_data', 'updated_at'])
