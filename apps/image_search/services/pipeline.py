"""Provider-neutral image search with separate identity and quality decisions."""

import hashlib
import logging
import time
from datetime import timedelta
from typing import TypedDict

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils.timezone import now

from apps.image_search.models import ImageSearchCache, ImageSearchLog
from apps.image_search.services.candidate_filter import candidate_metadata_assessment
from apps.image_search.services.quality import score
from apps.image_search.services.query_builder import (
    QUERY_BUILDER_VERSION, _trusted_cross_codes, _trusted_fitments,
)
from apps.image_search.sources.base import ImageSearchOutcomeUncertain
from apps.image_search.sources.connection import image_search_domain_reference
from apps.image_search.sources.registry import (
    build_image_search_workflow_snapshot,
    get_workflow_sources,
)
from apps.products.models import ProductImage
from apps.products.storage import PhotoUploadPipeline
from apps.web_research.providers.base import WebSearchProviderError

logger = logging.getLogger(__name__)

PIPELINE_VERSION = 'v3'


class _QueryMetric(TypedDict):
    query: str
    confidence: str
    results_count: int
    metadata_pass_count: int
    accepted_count: int


def bounded_image_search_result(outcome: dict) -> dict:
    """Return the small, non-secret terminal payload stored on task tracking."""
    errors = outcome.get('errors')
    if not isinstance(errors, list):
        errors = []
    image_ids = outcome.get('product_image_ids')
    if not isinstance(image_ids, list):
        image_ids = []
    sources = outcome.get('sources')
    if not isinstance(sources, list):
        sources = []
    return {
        'reason_code': str(outcome.get('reason_code') or 'completed')[:80],
        'message': str(outcome.get('message') or '')[:500],
        'saved_count': max(0, int(outcome.get('saved_count') or 0)),
        'found_count': max(0, int(outcome.get('found_count') or 0)),
        'rejected_count': max(0, int(outcome.get('rejected_count') or 0)),
        'eligible_count': max(0, int(outcome.get('eligible_count') or 0)),
        'download_failed_count': max(
            0,
            int(outcome.get('download_failed_count') or 0),
        ),
        'sources': [str(value)[:50] for value in sources[:20]],
        'errors': [
            {
                'source_id': str(error.get('source_id') or '')[:50],
                'code': str(error.get('code') or '')[:80],
                'message': str(error.get('message') or '')[:500],
            }
            for error in errors[:20]
            if isinstance(error, dict)
        ],
        'cached': bool(outcome.get('cached', False)),
        'product_image_ids': [
            int(value)
            for value in image_ids[:20]
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ],
    }


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


def _finalize_candidate_image(
    image_id: int,
    *,
    source_id: str,
    tier: int,
    quality_score: float,
    search_confidence: str,
    low_quality: bool,
) -> ProductImage | None:
    """Apply provider metadata without racing human moderation."""

    from apps.products.feed_writers import (
        StaleProductFeedWrite,
        locked_product_images_feed_write,
    )

    for _attempt in range(3):
        current = ProductImage.objects.filter(pk=image_id).only(
            'pk', 'product_id',
        ).first()
        if current is None:
            return None
        try:
            with locked_product_images_feed_write(
                current.product_id,
                bump=False,
            ) as (_product, images):
                locked = images.get(image_id)
                if locked is None:
                    raise StaleProductFeedWrite(
                        f'Product image {image_id} disappeared during finalization.',
                    )
                if locked.status not in {
                    ProductImage.Status.NEEDS_REVIEW,
                    ProductImage.Status.LOW_CONFIDENCE,
                }:
                    return locked
                locked.source_id = source_id
                locked.tier = tier
                locked.quality_score = quality_score
                locked.search_confidence = search_confidence
                locked.status = (
                    ProductImage.Status.LOW_CONFIDENCE
                    if low_quality else ProductImage.Status.NEEDS_REVIEW
                )
                locked.save(update_fields=[
                    'source_id', 'tier', 'quality_score',
                    'search_confidence', 'status',
                ])
                return locked
        except StaleProductFeedWrite:
            continue
    raise StaleProductFeedWrite(
        f'Product image {image_id} changed repeatedly during finalization.',
    )


def run_for_product(
    product,
    *,
    workflow_key: str,
    tracking_id: int | None = None,
) -> dict:
    """Own one stable image workflow across provider checkpoints and apply."""
    from apps.core.advisory_lock import try_session_advisory_lock
    from apps.core.dispatch import SafeRetryableDispatchError

    with try_session_advisory_lock(f'image-search:{workflow_key}') as acquired:
        if not acquired:
            raise SafeRetryableDispatchError(
                'Image-search workflow is already owned by another worker.',
            )
        return _run_for_product_owned(
            product,
            workflow_key=workflow_key,
            tracking_id=tracking_id,
        )


def _run_for_product_owned(
    product,
    *,
    workflow_key: str,
    tracking_id: int | None = None,
) -> dict:
    """Search, validate identity, retain correct low-quality photos, and explain the result."""
    from apps.web_research.accounting import (
        acknowledge_web_search_workflow,
        acquire_web_search_workflow,
        release_empty_web_search_workflow,
        resume_web_search_workflow,
    )
    from apps.web_research.models import WebSearchWorkflow
    from apps.image_search.models import ImageSearchTask

    def persist_terminal_outcome(outcome: dict) -> None:
        if tracking_id is None:
            return
        ImageSearchTask.objects.filter(pk=tracking_id).update(
            status=ImageSearchTask.Status.SUCCEEDED,
            result=bounded_image_search_result(outcome),
            error_code='',
            error_message='',
            finished_at=now(),
            updated_at=now(),
        )

    def persist_source_log(source, **values) -> None:
        raw_plan = getattr(source, 'workflow_plan', None)
        source_index = (
            raw_plan.get('source_index') if isinstance(raw_plan, dict) else None
        )
        if not isinstance(source_index, int) or isinstance(source_index, bool):
            raise ImageSearchOutcomeUncertain(
                'Логический слот поиска изображений повреждён; '
                'требуется сверка.',
                code='provider_request_conflict',
            )
        ImageSearchLog.objects.update_or_create(
            workflow_key=workflow_key,
            workflow_slot=f'source:{source_index}',
            defaults={
                'tenant': product.tenant,
                'product': product,
                'source_id': source.source_id,
                **values,
            },
        )

    try:
        # The owner and workflow are acquired under the same tenant lock used
        # by model-level delete guards. Either the task is deleted first and
        # no workflow can be created, or the active workflow becomes visible
        # before a hard delete can proceed.
        with transaction.atomic():
            type(product.tenant).objects.select_for_update().only('pk').get(
                pk=product.tenant_id,
            )
            if tracking_id is not None:
                ImageSearchTask.objects.select_for_update().get(
                    pk=tracking_id,
                    tenant_id=product.tenant_id,
                    product_id=product.pk,
                )
            try:
                workflow = resume_web_search_workflow(
                    tenant=product.tenant,
                    operation='image_search',
                    workflow_key=workflow_key,
                )
            except WebSearchWorkflow.DoesNotExist:
                snapshot = build_image_search_workflow_snapshot(product)
                snapshot['cache_key'] = build_cache_key(product)
                snapshot['pipeline'] = {
                    'version': PIPELINE_VERSION,
                    'query_builder_version': QUERY_BUILDER_VERSION,
                    'max_images_per_product': int(
                        settings.IMAGE_SEARCH_SETTINGS['MAX_IMAGES_PER_PRODUCT'],
                    ),
                    'min_quality_score': float(
                        settings.IMAGE_SEARCH_SETTINGS['MIN_QUALITY_SCORE'],
                    ),
                    'min_resolution': int(
                        settings.IMAGE_SEARCH_SETTINGS['MIN_RESOLUTION'],
                    ),
                    'cache_ttl_days': int(
                        settings.IMAGE_SEARCH_SETTINGS['CACHE_TTL_DAYS'],
                    ),
                }
                workflow = acquire_web_search_workflow(
                    tenant=product.tenant,
                    operation='image_search',
                    domain_reference=image_search_domain_reference(product),
                    workflow_key=workflow_key,
                    input_snapshot=snapshot,
                    product=product,
                )
    except WebSearchProviderError as exc:
        if exc.outcome_uncertain or exc.code == 'provider_reconciliation_required':
            raise ImageSearchOutcomeUncertain(
                'Предыдущий платный поиск изображений '
                'требует сверки; автоматический повтор запрещён.',
                code=exc.code,
            ) from exc
        raise
    snapshot = workflow.input_snapshot
    pipeline_plan = (
        snapshot.get('pipeline') if isinstance(snapshot, dict) else None
    )
    if not isinstance(pipeline_plan, dict):
        raise ImageSearchOutcomeUncertain(
            'Неизменяемый план поиска изображений повреждён; '
            'требуется сверка.',
            code='provider_request_conflict',
        )
    max_images = int(pipeline_plan['max_images_per_product'])
    min_quality = float(pipeline_plan['min_quality_score'])
    min_resolution = int(pipeline_plan['min_resolution'])
    cache_ttl_days = int(pipeline_plan['cache_ttl_days'])
    cache_key = snapshot.get('cache_key')
    if not isinstance(cache_key, str) or not cache_key:
        raise ImageSearchOutcomeUncertain(
            'Неизменяемый план поиска изображений повреждён; '
            'требуется сверка.',
            code='provider_request_conflict',
        )
    consumed_attempt_ids: set[int] = set()

    def consume_existing_attempts_without_network() -> None:
        """Validate/consume only rows already reserved by this workflow.

        A crash may have saved enough ProductImage rows to fill the product
        before the final ACK transaction.  Re-running a source in that state
        would walk planned-but-never-started slots and could buy unnecessary
        searches.  Replaying exact persisted attempts proves their encrypted
        checkpoints (or recorded safe failures) without resolving credentials
        or crossing a provider boundary.
        """
        from apps.web_research.accounting import (
            WebSearchExecution,
            replay_recorded_web_search,
        )

        for attempt in workflow.attempts.order_by('pk'):
            try:
                execution: WebSearchExecution[object] | None = (
                    replay_recorded_web_search(
                        workflow,
                        call_key=attempt.call_key,
                        request_fingerprint=attempt.request_fingerprint,
                    )
                )
            except WebSearchProviderError as exc:
                attempt_id = getattr(exc, 'attempt_id', None)
                if isinstance(attempt_id, int) and not isinstance(attempt_id, bool):
                    consumed_attempt_ids.add(attempt_id)
                if exc.outcome_uncertain:
                    raise ImageSearchOutcomeUncertain(
                        'Исход платного поиска изображений неизвестен; '
                        'автоматический повтор запрещён.',
                        code=exc.code,
                    ) from exc
                continue
            if execution is None:
                raise ImageSearchOutcomeUncertain(
                    'Сохранённое свидетельство платного запроса '
                    'отсутствует.',
                    code='provider_checkpoint_missing',
                )
            consumed_attempt_ids.add(execution.attempt_id)

    def close_workflow_after_local_apply() -> None:
        if workflow.status == WebSearchWorkflow.Status.APPLIED:
            return
        if consumed_attempt_ids:
            acknowledge_web_search_workflow(
                workflow.pk,
                consumed_attempt_ids=consumed_attempt_ids,
            )
        else:
            release_empty_web_search_workflow(workflow.pk)

    cached = ImageSearchCache.objects.filter(
        cache_key=cache_key, expires_at__gt=now(),
    ).first()
    workflow_has_attempts = workflow.attempts.exists()
    if cached and not workflow_has_attempts:
        outcome = dict(cached.results or {})
        outcome.update({'cached': True})
        outcome.setdefault('saved_count', outcome.get('count', 0))
        outcome.setdefault(
            'message',
            'Использован недавний результат поиска фотографий.',
        )
        with transaction.atomic():
            persist_terminal_outcome(outcome)
            close_workflow_after_local_apply()
        return outcome

    total_non_rejected = ProductImage.objects.filter(product=product).exclude(
        status=ProductImage.Status.REJECTED,
    ).count()
    from apps.products.storage import MAX_PHOTOS
    # Every retained non-rejected image consumes the workflow's configured
    # target, including NEEDS_REVIEW/LOW_CONFIDENCE rows saved immediately
    # before a worker crash. Counting only publishable rows would replay the
    # paid checkpoint into the upload pipeline, which correctly deduplicates
    # the existing object but then reports a false ``download_failed`` result.
    remaining_slots = min(
        max_images - total_non_rejected,
        MAX_PHOTOS - total_non_rejected,
    )
    if remaining_slots <= 0:
        if workflow_has_attempts:
            consume_existing_attempts_without_network()
        outcome = _outcome(
            reason_code='already_has_images',
            message='Поиск не запускался: у товара уже достаточно фотографий.',
            product_image_ids=list(
                ProductImage.objects.filter(product=product)
                .exclude(status=ProductImage.Status.REJECTED)
                .order_by('pk')
                .values_list('pk', flat=True)[:20]
            ),
        )
        with transaction.atomic():
            persist_terminal_outcome(outcome)
            close_workflow_after_local_apply()
        return outcome

    saved: list[ProductImage] = []
    uploader = PhotoUploadPipeline()
    sources = get_workflow_sources(
        product,
        workflow,
        consumed_attempt_ids=consumed_attempt_ids,
    )
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
        queries = source.planned_queries()
        primary_query, primary_confidence = queries[0] if queries else ('', '')
        error = ''

        try:
            candidates = source.search()
            error = source.last_error
        except ImageSearchOutcomeUncertain as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            attempted_query = str(
                getattr(source, 'last_attempt_query', '') or primary_query,
            )[:500]
            attempted_confidence = next(
                (
                    confidence
                    for query, confidence in queries
                    if query == attempted_query
                ),
                primary_confidence,
            )
            persist_source_log(
                source,
                query=attempted_query,
                confidence=attempted_confidence,
                duration_ms=duration_ms,
                outcome=ImageSearchLog.Outcome.OUTCOME_UNCERTAIN,
                error_code=exc.code,
                error=str(exc)[:2000],
                query_builder_version=QUERY_BUILDER_VERSION,
            )
            raise
        except WebSearchProviderError as exc:
            if exc.outcome_uncertain or exc.code == 'provider_reconciliation_required':
                duration_ms = int((time.monotonic() - t0) * 1000)
                attempted_query = str(
                    getattr(source, 'last_attempt_query', '') or primary_query,
                )[:500]
                persist_source_log(
                    source,
                    query=attempted_query,
                    confidence=primary_confidence,
                    duration_ms=duration_ms,
                    outcome=ImageSearchLog.Outcome.OUTCOME_UNCERTAIN,
                    error_code=exc.code,
                    error=(
                        'Предыдущий платный запрос требует ручной сверки; '
                        'новый запрос провайдеру не отправлен.'
                        if exc.code == 'provider_reconciliation_required'
                        else (
                            'Исход платного запроса неизвестен; '
                            'повтор запрещён.'
                        )
                    ),
                    query_builder_version=QUERY_BUILDER_VERSION,
                )
                raise ImageSearchOutcomeUncertain(
                    (
                        'Предыдущий платный поиск изображений требует сверки; '
                        'автоматический повтор запрещён.'
                        if exc.code == 'provider_reconciliation_required'
                        else 'Исход платного поиска изображений неизвестен; '
                        'автоматический повтор запрещён.'
                    ),
                    code=exc.code,
                ) from exc
            error = str(exc)
            source.last_error_code = exc.code
            logger.warning('[pipeline] %s отказ: %s', source.source_id, exc.code)
        except DatabaseError:
            # Managed connection state is authoritative. A database failure
            # must never be interpreted as an absent row/env fallback.
            raise
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
        query_metrics: dict[str, _QueryMetric] = {
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
            query = str(candidate.raw_meta.get('query') or '')
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
                query = str(candidate.raw_meta.get('query') or '')
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
                status=ProductImage.Status.NEEDS_REVIEW,
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

            image = _finalize_candidate_image(
                image.pk,
                source_id=candidate.source_id,
                tier=candidate.tier,
                quality_score=candidate.quality_score,
                search_confidence=candidate.raw_meta.get('confidence', '').lower(),
                low_quality=low_quality,
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

            saved.append(image)
            accepted += 1
            query = str(candidate.raw_meta.get('query') or '')
            if query in query_metrics:
                query_metrics[query]['accepted_count'] += 1
            _record_candidate_assessments(
                product, [candidate], verdict='review', product_image=image,
            )

        persist_source_log(
            source,
            query=primary_query,
            confidence=primary_confidence,
            results_count=len(candidates),
            accepted_count=accepted,
            duration_ms=duration_ms,
            outcome=(
                ImageSearchLog.Outcome.SAFE_FAILURE
                if error else ImageSearchLog.Outcome.COMPLETED
            ),
            error_code=source.last_error_code if error else '',
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
    cache_ttl = timedelta(days=cache_ttl_days) if saved else timedelta(hours=1)
    # ACK and the final local domain markers commit together. If the worker is
    # lost earlier, the workflow remains active and the same tracking task can
    # restore provider checkpoints without another paid request.
    with transaction.atomic():
        ImageSearchCache.objects.update_or_create(
            cache_key=cache_key,
            defaults={'results': outcome, 'expires_at': now() + cache_ttl},
        )
        product.image_status = 'has_images' if saved else 'no_image'
        product.save(update_fields=['image_status'])
        persist_terminal_outcome(outcome)
        close_workflow_after_local_apply()
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
    for candidate in candidates:
        ImageAssessment.objects.update_or_create(
            tenant=product.tenant,
            product=product,
            product_image=product_image,
            source_url=candidate.url,
            provider_id='metadata_identity_rules',
            model_id=PIPELINE_VERSION,
            verdict=verdict,
            defaults={
                'source_id': candidate.source_id,
                'score': candidate.raw_meta.get('identity_score'),
                'reason_codes': candidate.raw_meta.get(
                    'assessment_reasons',
                    [],
                ),
                'checks': {
                    'title': candidate.raw_meta.get('title', ''),
                    'description': candidate.raw_meta.get('description', ''),
                    'query': candidate.raw_meta.get('query', ''),
                    'identity_score': candidate.raw_meta.get('identity_score'),
                    'technical_quality_score': candidate.quality_score,
                },
                'expected_product': expected,
            },
        )
