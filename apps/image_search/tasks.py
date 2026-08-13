"""Celery-задачи для поиска изображений."""

import logging

from celery import shared_task
from django.core.cache import caches
from django_redis.cache import RedisCache
from django.utils.timezone import now

logger = logging.getLogger(__name__)
cache = caches['coordination']

# Максимальное время удержания лока (секунды) — перекрывает максимальный pipeline run
_LOCK_TIMEOUT = 300


@shared_task(
    bind=True,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=120,
    queue='image_search',
    ignore_result=False,
)
def search_images_for_product(
    self,
    product_id: int,
    tracking_id: int | None = None,
) -> dict:
    """Запускает поиск изображений для товара через каскадный pipeline.

    Идемпотентна: повторный вызов во время выполнения откладывается через
    durable dispatch, а provider workflow восстанавливает уже оплаченные ответы
    без повторной сети.

    Пользовательские вызовы должны создавать BackgroundJobDispatch в той же
    транзакции, что и tracking/domain record.

    Args:
        product_id: ID товара в БД.
        tracking_id: Durable ImageSearchTask primary key and workflow owner.
    """
    from apps.image_search.models import ImageSearchTask
    from apps.image_search.services.pipeline import run_for_product
    from apps.core.dispatch import SafeRetryableDispatchError
    from apps.products.models import Product

    lock_key = f'lock:img_search:{product_id}'
    if not isinstance(cache, RedisCache):
        raise RuntimeError('Image search coordination cache must be RedisCache.')
    lock = cache.lock(lock_key, timeout=_LOCK_TIMEOUT)

    if not lock.acquire(blocking=False):
        logger.info(f'[img_search] задача уже выполняется для product_id={product_id}')
        raise SafeRetryableDispatchError(
            'Image search for this product is already running.',
        )

    try:
        try:
            product = Product.objects.select_related('tenant').get(pk=product_id)
        except Product.DoesNotExist:
            logger.warning(f'[img_search] Product {product_id} не найден')
            return {
                'reason_code': 'product_not_found', 'saved_count': 0,
                'message': 'Товар не найден.',
            }

        # Paid calls require a durable identity that survives worker loss.
        # Legacy product-only dispatches fail before provider I/O and can be
        # safely resubmitted through the API to obtain a tracked workflow.
        if not tracking_id:
            raise SafeRetryableDispatchError(
                'Image search dispatch has no durable tracking workflow.',
            )
        tracking = ImageSearchTask.objects.filter(
            pk=tracking_id,
            tenant_id=product.tenant_id,
            product_id=product.pk,
        ).only('pk', 'status', 'result').first()
        if tracking is None:
            raise SafeRetryableDispatchError(
                'Image search tracking workflow does not match the product.',
            )

        if tracking.status == ImageSearchTask.Status.SUCCEEDED:
            return dict(tracking.result or {})
        ImageSearchTask.objects.filter(
            pk=tracking.pk,
            status__in=[
                ImageSearchTask.Status.PENDING,
                ImageSearchTask.Status.FAILED,
            ],
        ).update(
            status=ImageSearchTask.Status.RUNNING,
            error_code='',
            error_message='',
            finished_at=None,
            updated_at=now(),
        )

        # Slot/cache checks live inside the workflow-aware pipeline. Keeping
        # them there is important for crash recovery: an already-persisted
        # ProductImage may be the evidence that lets a replay ACK the original
        # paid workflow without sending another provider request.
        return run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    except Exception as exc:
        if tracking_id and bool(getattr(exc, 'outcome_uncertain', False)):
            ImageSearchTask.objects.filter(
                pk=tracking_id,
                status__in=[
                    ImageSearchTask.Status.PENDING,
                    ImageSearchTask.Status.RUNNING,
                    ImageSearchTask.Status.FAILED,
                    ImageSearchTask.Status.RECONCILIATION_REQUIRED,
                ],
            ).update(
                status=ImageSearchTask.Status.RECONCILIATION_REQUIRED,
                error_code=str(
                    getattr(exc, 'code', '')
                    or 'provider_reconciliation_required'
                )[:80],
                error_message=(
                    'Paid provider outcome requires operator reconciliation.'
                ),
                finished_at=now(),
                updated_at=now(),
            )
        logger.error(
            f'[img_search] ошибка для product_id={product_id}: {exc}', exc_info=True,
        )
        raise

    finally:
        try:
            lock.release()
        except Exception:
            pass
