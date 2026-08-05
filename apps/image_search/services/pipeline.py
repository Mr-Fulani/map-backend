"""Provider-neutral image search with separate identity and quality decisions."""

import hashlib
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from apps.image_search.models import ImageSearchCache, ImageSearchLog
from apps.image_search.services.candidate_filter import candidate_metadata_assessment
from apps.image_search.services.quality import score
from apps.image_search.services.query_builder import (
    QUERY_BUILDER_VERSION, _trusted_cross_codes, _trusted_fitments,
)
from apps.image_search.sources.registry import get_active_sources
from apps.products.models import ProductImage
from apps.products.storage import PhotoUploadPipeline

logger = logging.getLogger(__name__)

PIPELINE_VERSION = 'v3'


def build_cache_key(product) -> str:
    """Include enriched identity so approving OEM/fitment invalidates stale misses."""
    cross_codes = ','.join(code.upper() for _, code in _trusted_cross_codes(product))
    fitments = ','.join(value.upper() for value in _trusted_fitments(product))
    payload = '|'.join([
        PIPELINE_VERSION,
        QUERY_BUILDER_VERSION,
        str(product.tenant_id),
        str(product.pk),
        str(product.article or '').strip().upper(),
        str(product.brand or '').strip().upper(),
        str(product.catalog_category_id or ''),
        cross_codes,
        fitments,
    ])
    return f'img_search:{PIPELINE_VERSION}:{hashlib.sha256(payload.encode()).hexdigest()}'


def run_for_product(product) -> dict:
    """Search, validate identity, retain correct low-quality photos, and explain the result."""
    cfg = settings.IMAGE_SEARCH_SETTINGS
    max_images = cfg['MAX_IMAGES_PER_PRODUCT']
    min_quality = cfg['MIN_QUALITY_SCORE']
    min_resolution = cfg['MIN_RESOLUTION']
    cache_key = build_cache_key(product)

    cached = ImageSearchCache.objects.filter(
        cache_key=cache_key, expires_at__gt=now(),
    ).first()
    if cached:
        outcome = dict(cached.results or {})
        outcome.update({'cached': True})
        outcome.setdefault('saved_count', outcome.get('count', 0))
        outcome.setdefault('message', 'Использован недавний результат поиска фотографий.')
        return outcome

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
        return _outcome(
            reason_code='already_has_images',
            message='Поиск не запускался: у товара уже достаточно фотографий.',
        )

    saved: list[ProductImage] = []
    uploader = PhotoUploadPipeline()
    sources = get_active_sources(product)
    found_count = 0
    rejected_count = 0
    eligible_count = 0
    download_failed_count = 0
    errors: list[dict] = []
    attempted_sources: list[str] = []
    rejected_urls = set(
        product.images.filter(status=ProductImage.Status.REJECTED)
        .exclude(url_source='').values_list('url_source', flat=True)
    )

    for source in sources:
        if len(saved) >= remaining_slots:
            break
        attempted_sources.append(source.source_id)
        t0 = time.monotonic()
        candidates = []
        queries = source.build_queries()[:source.max_queries]
        primary_query, primary_confidence = queries[0] if queries else ('', '')
        error = ''

        try:
            candidates = source.search()
            error = source.last_error
        except Exception as exc:
            error = str(exc)
            source.last_error_code = 'source_error'
            logger.warning('[pipeline] %s ошибка: %s', source.source_id, exc)
        if error:
            errors.append({
                'source_id': source.source_id,
                'code': source.last_error_code or 'source_error',
                'message': error,
            })

        duration_ms = int((time.monotonic() - t0) * 1000)
        found_count += len(candidates)
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

        eligible = []
        rejected = []
        for candidate in candidates:
            allowed, reasons, identity_score = candidate_metadata_assessment(product, candidate)
            candidate.raw_meta['identity_score'] = identity_score
            candidate.raw_meta['assessment_reasons'] = reasons
            candidate.quality_score = score(candidate)
            if allowed:
                eligible.append(candidate)
                query = candidate.raw_meta.get('query', '')
                if query in query_metrics:
                    query_metrics[query]['metadata_pass_count'] += 1
            else:
                rejected.append(candidate)

        rejected_count += len(rejected)
        eligible_count += len(eligible)
        _record_candidate_assessments(product, rejected, verdict='reject')

        ranked = sorted(
            eligible,
            key=lambda candidate: (
                float(candidate.raw_meta.get('identity_score', 0) or 0),
                candidate.quality_score,
            ),
            reverse=True,
        )
        accepted = 0
        for candidate in ranked:
            if len(saved) >= remaining_slots:
                break
            if candidate.url in rejected_urls:
                continue

            image = uploader.process(
                candidate.url,
                product,
                source_id=candidate.source_id,
                validate_quality=True,
                allow_low_resolution=True,
            )
            if image is None:
                download_failed_count += 1
                continue
            if image.status == ProductImage.Status.REJECTED:
                rejected_urls.add(candidate.url)
                continue
            if image.status in (
                ProductImage.Status.AUTO_APPROVED,
                ProductImage.Status.MANUALLY_SET,
                ProductImage.Status.IMPORTED,
            ):
                continue

            low_quality = (
                min(image.resolution_w or 0, image.resolution_h or 0) < min_resolution
                or candidate.quality_score < min_quality
            )
            if low_quality:
                candidate.raw_meta['assessment_reasons'] = [
                    *candidate.raw_meta.get('assessment_reasons', []),
                    'needs_media_processing',
                ]

            image.source_id = candidate.source_id
            image.tier = candidate.tier
            image.quality_score = candidate.quality_score
            image.search_confidence = candidate.raw_meta.get('confidence', '').lower()
            image.status = (
                ProductImage.Status.LOW_CONFIDENCE
                if low_quality else ProductImage.Status.NEEDS_REVIEW
            )
            image.save(update_fields=[
                'source_id', 'tier', 'quality_score', 'search_confidence', 'status',
            ])

            saved.append(image)
            accepted += 1
            query = candidate.raw_meta.get('query', '')
            if query in query_metrics:
                query_metrics[query]['accepted_count'] += 1
            _record_candidate_assessments(
                product, [candidate], verdict='review', product_image=image,
            )

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

    outcome = _final_outcome(
        saved=saved,
        found_count=found_count,
        rejected_count=rejected_count,
        eligible_count=eligible_count,
        download_failed_count=download_failed_count,
        attempted_sources=attempted_sources,
        errors=errors,
    )
    cache_ttl = timedelta(days=cfg['CACHE_TTL_DAYS']) if saved else timedelta(hours=1)
    ImageSearchCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={'results': outcome, 'expires_at': now() + cache_ttl},
    )
    product.image_status = 'has_images' if saved else 'no_image'
    product.save(update_fields=['image_status'])
    return outcome


def _outcome(*, reason_code: str, message: str, **counts) -> dict:
    return {
        'reason_code': reason_code,
        'message': message,
        'saved_count': 0,
        'found_count': 0,
        'rejected_count': 0,
        'eligible_count': 0,
        'download_failed_count': 0,
        'sources': [],
        'errors': [],
        'cached': False,
        **counts,
    }


def _final_outcome(
    *, saved, found_count, rejected_count, eligible_count,
    download_failed_count, attempted_sources, errors,
) -> dict:
    if saved:
        low_quality_count = sum(
            image.status == ProductImage.Status.LOW_CONFIDENCE for image in saved
        )
        message = f'Сохранено фотографий: {len(saved)}.'
        if low_quality_count:
            message += f' Требуют улучшения качества: {low_quality_count}.'
        reason_code = 'found'
    elif not attempted_sources:
        reason_code = 'no_sources'
        message = 'Нет доступных сервисов поиска фотографий. Проверьте подключения в админке.'
    elif found_count and rejected_count >= found_count:
        reason_code = 'rejected_by_relevance'
        message = (
            f'Сервисы нашли {found_count} изображений, но все отклонены: '
            'они не соответствуют товару, OEM или применяемости.'
        )
    elif eligible_count and download_failed_count:
        reason_code = 'download_failed'
        message = (
            f'Найдено подходящих изображений: {eligible_count}, но сайты не дали '
            'безопасно скачать файлы. Можно повторить поиск или загрузить фото вручную.'
        )
    elif errors and not found_count:
        reason_code = 'source_error'
        message = 'Сервисы поиска временно недоступны: ' + '; '.join(
            error['message'] for error in errors
        )
    else:
        reason_code = 'no_results'
        message = 'Сервисы не нашли изображений по подтверждённым данным товара.'

    return _outcome(
        reason_code=reason_code,
        message=message,
        saved_count=len(saved),
        found_count=found_count,
        rejected_count=rejected_count,
        eligible_count=eligible_count,
        download_failed_count=download_failed_count,
        sources=attempted_sources,
        errors=errors,
        product_image_ids=[image.pk for image in saved],
    )


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
        'trusted_cross_codes': [code for _, code in _trusted_cross_codes(product)],
        'fitments': _trusted_fitments(product),
    }
    ImageAssessment.objects.bulk_create([
        ImageAssessment(
            tenant=product.tenant,
            product=product,
            product_image=product_image,
            source_url=candidate.url,
            source_id=candidate.source_id,
            provider_id='metadata_identity_rules',
            model_id=PIPELINE_VERSION,
            verdict=verdict,
            score=candidate.raw_meta.get('identity_score'),
            reason_codes=candidate.raw_meta.get('assessment_reasons', []),
            checks={
                'title': candidate.raw_meta.get('title', ''),
                'description': candidate.raw_meta.get('description', ''),
                'query': candidate.raw_meta.get('query', ''),
                'identity_score': candidate.raw_meta.get('identity_score'),
                'technical_quality_score': candidate.quality_score,
            },
            expected_product=expected,
        )
        for candidate in candidates
    ])
