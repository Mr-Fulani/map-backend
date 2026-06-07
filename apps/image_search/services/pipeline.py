"""Конвейер поиска изображений для товара.

Каскадный обход источников по приоритету (tier) с ранней остановкой
при достижении MAX_IMAGES_PER_PRODUCT хороших кандидатов.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from apps.image_search.models import ImageSearchCache, ImageSearchLog
from apps.image_search.services.quality import score
from apps.image_search.sources.registry import get_active_sources
from apps.products.models import ProductImage
from apps.products.storage import PhotoUploadPipeline

logger = logging.getLogger(__name__)


def run_for_product(product) -> list[ProductImage]:
    """Запускает каскадный поиск изображений для товара.

    Алгоритм:
    1. Проверяет кеш — если есть свежий, завершает без запросов.
    2. Перебирает источники по tier (меньше = выше приоритет).
    3. Для каждого источника: получает кандидатов, скорит, фильтрует.
    4. Скачивает подходящих через PhotoUploadPipeline (SHA256-дедупликация).
    5. Проставляет поля image_search в ProductImage.
    6. Логирует в ImageSearchLog, кеширует результат.
    7. Обновляет image_status товара.

    Args:
        product: Экземпляр Product со связанным tenant.

    Returns:
        Список сохранённых ProductImage.
    """
    cfg = settings.IMAGE_SEARCH_SETTINGS
    max_images = cfg['MAX_IMAGES_PER_PRODUCT']
    min_score = cfg['MIN_QUALITY_SCORE']
    auto_approve_thresh = cfg['AUTO_APPROVE_THRESHOLD']
    cache_ttl_days = cfg['CACHE_TTL_DAYS']

    cache_key = f'img_search:{product.article}:{product.brand}'

    # Ранний выход если кеш актуален
    if ImageSearchCache.objects.filter(cache_key=cache_key, expires_at__gt=now()).exists():
        logger.debug(f'[pipeline] кеш актуален: {cache_key}')
        return []

    saved: list[ProductImage] = []
    uploader = PhotoUploadPipeline()
    sources = get_active_sources(product)

    for source in sources:
        if len(saved) >= max_images:
            break

        t0 = time.monotonic()
        candidates = []
        primary_query = ''
        primary_confidence = ''
        error = ''

        try:
            queries = source.build_queries()
            if queries:
                primary_query, primary_confidence = queries[0]
            candidates = source.search()
        except Exception as exc:
            error = str(exc)
            logger.warning(f'[pipeline] {source.source_id} ошибка: {exc}')

        duration_ms = int((time.monotonic() - t0) * 1000)

        # Скорить кандидатов и отфильтровать по порогу
        for c in candidates:
            c.quality_score = score(c)

        good = sorted(
            [c for c in candidates if c.quality_score >= min_score],
            key=lambda c: c.quality_score,
            reverse=True,
        )

        accepted = 0
        for candidate in good:
            if len(saved) >= max_images:
                break

            pi = uploader.process(candidate.url, product)
            if pi is None:
                continue

            # Статус зависит от скора: выше порога = авто-одобрено
            status = (
                ProductImage.Status.AUTO_APPROVED
                if candidate.quality_score >= auto_approve_thresh
                else ProductImage.Status.NEEDS_REVIEW
            )

            pi.source_id = candidate.source_id
            pi.tier = candidate.tier
            pi.quality_score = candidate.quality_score
            pi.search_confidence = candidate.raw_meta.get('confidence', '').lower()
            pi.status = status
            if candidate.width:
                pi.resolution_w = candidate.width
            if candidate.height:
                pi.resolution_h = candidate.height

            pi.save(update_fields=[
                'source_id', 'tier', 'quality_score', 'search_confidence',
                'status', 'resolution_w', 'resolution_h',
            ])

            saved.append(pi)
            accepted += 1

        # Логируем результат работы источника
        ImageSearchLog.objects.create(
            tenant=product.tenant,
            product=product,
            source_id=source.source_id,
            query=primary_query,
            confidence=primary_confidence,
            results_count=len(candidates),
            accepted_count=accepted,
            duration_ms=duration_ms,
            error=error,
        )

    # Обновляем кеш. Пустой результат кешируем на 1 час чтобы не блокировать повторные попытки.
    cache_ttl = timedelta(days=cache_ttl_days) if saved else timedelta(hours=1)
    ImageSearchCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            'results': {
                'product_image_ids': [pi.pk for pi in saved],
                'count': len(saved),
            },
            'expires_at': now() + cache_ttl,
        },
    )

    # Обновляем статус изображений у товара
    product.image_status = 'has_images' if saved else 'no_image'
    product.save(update_fields=['image_status'])

    return saved
