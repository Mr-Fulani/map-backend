"""Celery-задачи для поиска изображений."""

import logging

from celery import shared_task
from django.core.cache import cache
from django.utils.timezone import now

logger = logging.getLogger(__name__)

# Максимальное время удержания лока (секунды) — перекрывает максимальный pipeline run
_LOCK_TIMEOUT = 300


@shared_task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=120)
def search_images_for_product(self, product_id: int) -> dict:
    """Запускает поиск изображений для товара через каскадный pipeline.

    Идемпотентна: повторный вызов во время выполнения пропускается (Redis lock).
    Двойная проверка: пропускается если товар уже имеет достаточно фото.

    Запускать через transaction.on_commit чтобы гарантировать видимость Product:
        transaction.on_commit(lambda: search_images_for_product.delay(product.pk))

    Args:
        product_id: ID товара в БД.
    """
    from apps.image_search.services.pipeline import run_for_product
    from apps.products.models import Product

    lock_key = f'lock:img_search:{product_id}'
    lock = cache.lock(lock_key, timeout=_LOCK_TIMEOUT)

    if not lock.acquire(blocking=False):
        logger.info(f'[img_search] задача уже выполняется для product_id={product_id}')
        return {'result_code': 'already_running', 'saved_count': 0}

    try:
        try:
            product = Product.objects.select_related('tenant').get(pk=product_id)
        except Product.DoesNotExist:
            logger.warning(f'[img_search] Product {product_id} не найден')
            return {'result_code': 'product_not_found', 'saved_count': 0}

        # Двойная проверка — пропустить если уже достаточно принятых фото
        from django.conf import settings
        max_images = settings.IMAGE_SEARCH_SETTINGS['MAX_IMAGES_PER_PRODUCT']
        existing = product.images.filter(
            status__in=['imported', 'auto_approved', 'manually_set'],
        ).count()
        if existing >= max_images:
            logger.debug(f'[img_search] product {product_id} уже имеет {existing} фото')
            return {'result_code': 'already_has_images', 'saved_count': 0}

        started_at = now()
        saved = run_for_product(product)

        from apps.image_search.models import ImageSearchLog
        logs = list(ImageSearchLog.objects.filter(
            product=product,
            created_at__gte=started_at,
        ))
        candidates_count = sum(log.results_count for log in logs)
        metadata_pass_count = sum(
            metric.get('metadata_pass_count', 0)
            for log in logs
            for metric in log.query_metrics
        )
        saved_count = len(saved)

        if saved_count:
            result_code = 'found'
        elif not logs:
            result_code = 'no_sources'
        elif not candidates_count:
            result_code = 'no_candidates'
        elif not metadata_pass_count:
            result_code = 'filtered_out'
        else:
            result_code = 'rejected_after_validation'

        return {
            'result_code': result_code,
            'saved_count': saved_count,
            'candidates_count': candidates_count,
            'metadata_pass_count': metadata_pass_count,
        }

    except Exception as exc:
        logger.error(
            f'[img_search] ошибка для product_id={product_id}: {exc}', exc_info=True,
        )
        raise self.retry(exc=exc)

    finally:
        try:
            lock.release()
        except Exception:
            pass
