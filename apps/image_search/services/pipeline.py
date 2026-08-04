"""Конвейер поиска изображений для товара.

Каскадный обход источников по приоритету (tier) с ранней остановкой
при достижении MAX_IMAGES_PER_PRODUCT хороших кандидатов.
"""

import logging
import time
import hashlib
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from apps.image_search.models import ImageSearchCache, ImageSearchLog
from apps.image_search.services.quality import score
from apps.image_search.services.candidate_filter import candidate_metadata_assessment
from apps.image_search.services.query_builder import QUERY_BUILDER_VERSION
from apps.image_search.sources.registry import get_active_sources
from apps.products.models import ProductImage
from apps.products.storage import PhotoUploadPipeline

logger = logging.getLogger(__name__)

PIPELINE_VERSION = 'v2'


def build_cache_key(product) -> str:
    """Tenant/product/version scoped key; avoids cross-tenant and blank identity collisions."""
    payload = '|'.join([
        PIPELINE_VERSION,
        str(product.tenant_id),
        str(product.pk),
        str(product.article or '').strip().upper(),
        str(product.brand or '').strip().upper(),
        str(product.catalog_category_id or ''),
    ])
    return f'img_search:{PIPELINE_VERSION}:{hashlib.sha256(payload.encode()).hexdigest()}'


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
    cache_ttl_days = cfg['CACHE_TTL_DAYS']

    cache_key = build_cache_key(product)

    # Ранний выход если кеш актуален
    if ImageSearchCache.objects.filter(cache_key=cache_key, expires_at__gt=now()).exists():
        logger.debug(f'[pipeline] кеш актуален: {cache_key}')
        return []

    # Учитываем фото уже добавленные парсерами — не превышаем общий лимит
    publishable_count = ProductImage.objects.filter(
        product=product,
        status__in=(
            ProductImage.Status.AUTO_APPROVED,
            ProductImage.Status.MANUALLY_SET,
            ProductImage.Status.IMPORTED,
        ),
    ).count()
    total_non_rejected = ProductImage.objects.filter(product=product).exclude(
        status=ProductImage.Status.REJECTED,
    ).count()
    from apps.products.storage import MAX_PHOTOS
    remaining_slots = min(max_images - publishable_count, MAX_PHOTOS - total_non_rejected)
    if remaining_slots <= 0:
        logger.debug(
            '[pipeline] product=%s publishable=%s total=%s, image search пропущен',
            product.pk, publishable_count, total_non_rejected,
        )
        return []

    saved: list[ProductImage] = []
    uploader = PhotoUploadPipeline()
    sources = get_active_sources(product)

    # URL-уровень: не скачивать то, что уже было отклонено для этого товара
    rejected_urls: set[str] = set(
        product.images
        .filter(status=ProductImage.Status.REJECTED)
        .exclude(url_source='')
        .values_list('url_source', flat=True)
    )

    for source in sources:
        if len(saved) >= remaining_slots:
            break

        t0 = time.monotonic()
        candidates = []
        queries = []
        primary_query = ''
        primary_confidence = ''
        error = ''

        try:
            queries = source.build_queries()[:source.max_queries]
            if queries:
                primary_query, primary_confidence = queries[0]
            candidates = source.search()
        except Exception as exc:
            error = str(exc)
            logger.warning(f'[pipeline] {source.source_id} ошибка: {exc}')

        duration_ms = int((time.monotonic() - t0) * 1000)
        query_metrics = {
            query: {
                'query': query,
                'confidence': confidence,
                'results_count': 0,
                'metadata_pass_count': 0,
                'accepted_count': 0,
            }
            for query, confidence in queries
        }
        for candidate in candidates:
            query = candidate.raw_meta.get('query', '')
            if query in query_metrics:
                query_metrics[query]['results_count'] += 1

        # Search metadata is a cheap deterministic gate; semantic validation is a
        # separate provider-backed stage and can be added without changing sources.
        filtered_candidates = []
        rejected_assessments = []
        for c in candidates:
            allowed, reasons, relevance = candidate_metadata_assessment(product, c)
            c.raw_meta['metadata_relevance'] = relevance
            c.raw_meta['assessment_reasons'] = reasons
            if allowed:
                filtered_candidates.append(c)
                query = c.raw_meta.get('query', '')
                if query in query_metrics:
                    query_metrics[query]['metadata_pass_count'] += 1
            else:
                rejected_assessments.append(c)

        _record_candidate_assessments(product, rejected_assessments, verdict='reject')

        # Скорить кандидатов и отфильтровать по порогу
        for c in filtered_candidates:
            c.quality_score = score(c)

        good = sorted(
            [c for c in filtered_candidates if c.quality_score >= min_score],
            key=lambda c: c.quality_score,
            reverse=True,
        )

        accepted = 0
        for candidate in good:
            if len(saved) >= remaining_slots:
                break

            if candidate.url in rejected_urls:
                continue

            pi = uploader.process(candidate.url, product, validate_quality=True)
            if pi is None:
                continue

            # SHA256-уровень: пропустить если контент совпал с ранее отклонённым
            if pi.status == ProductImage.Status.REJECTED:
                rejected_urls.add(candidate.url)
                continue

            # Перцептивный/SHA-дубль уже одобренного, ручного или импортированного
            # фото — не понижаем его статус и не перетираем источник поиском.
            if pi.status in (
                ProductImage.Status.AUTO_APPROVED,
                ProductImage.Status.MANUALLY_SET,
                ProductImage.Status.IMPORTED,
            ):
                continue

            status = ProductImage.Status.NEEDS_REVIEW

            pi.source_id = candidate.source_id
            pi.tier = candidate.tier
            pi.quality_score = candidate.quality_score
            pi.search_confidence = candidate.raw_meta.get('confidence', '').lower()
            pi.status = status
            if candidate.width and not pi.resolution_w:
                pi.resolution_w = candidate.width
            if candidate.height and not pi.resolution_h:
                pi.resolution_h = candidate.height

            pi.save(update_fields=[
                'source_id', 'tier', 'quality_score', 'search_confidence',
                'status', 'resolution_w', 'resolution_h',
            ])

            saved.append(pi)
            accepted += 1
            query = candidate.raw_meta.get('query', '')
            if query in query_metrics:
                query_metrics[query]['accepted_count'] += 1

            _record_candidate_assessments(
                product, [candidate], verdict='review', product_image=pi,
            )

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
            query_metrics=list(query_metrics.values()),
            query_builder_version=QUERY_BUILDER_VERSION,
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


def _record_candidate_assessments(
    product,
    candidates,
    *,
    verdict: str,
    product_image=None,
) -> None:
    if not candidates:
        return
    from apps.media_processing.models import ImageAssessment

    expected = {
        'article': product.article,
        'brand': product.brand,
        'name': product.name,
        'category': product.category_1c,
        'catalog_category_id': product.catalog_category_id,
    }
    ImageAssessment.objects.bulk_create([
        ImageAssessment(
            tenant=product.tenant,
            product=product,
            product_image=product_image,
            source_url=candidate.url,
            source_id=candidate.source_id,
            provider_id='metadata_rules',
            model_id=PIPELINE_VERSION,
            verdict=verdict,
            score=candidate.raw_meta.get('metadata_relevance'),
            reason_codes=candidate.raw_meta.get('assessment_reasons', []),
            checks={
                'title': candidate.raw_meta.get('title', ''),
                'query': candidate.raw_meta.get('query', ''),
                'quality_score': candidate.quality_score,
            },
            expected_product=expected,
        )
        for candidate in candidates
    ])
