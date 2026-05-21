"""Celery-задачи для поиска изображений."""

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Максимальное время удержания лока (секунды) — перекрывает максимальный pipeline run
_LOCK_TIMEOUT = 300


@shared_task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=120)
def search_images_for_product(self, product_id: int) -> None:
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
        return

    try:
        try:
            product = Product.objects.select_related('tenant').get(pk=product_id)
        except Product.DoesNotExist:
            logger.warning(f'[img_search] Product {product_id} не найден')
            return

        # Двойная проверка — пропустить если уже достаточно принятых фото
        from django.conf import settings
        max_images = settings.IMAGE_SEARCH_SETTINGS['MAX_IMAGES_PER_PRODUCT']
        existing = product.images.filter(
            status__in=['imported', 'auto_approved', 'manually_set'],
        ).count()
        if existing >= max_images:
            logger.debug(f'[img_search] product {product_id} уже имеет {existing} фото')
            return

        run_for_product(product)

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
