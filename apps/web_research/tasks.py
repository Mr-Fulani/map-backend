from celery import shared_task
from django.core.cache import caches
from django.db import transaction
from django.db.models import Q
from django_redis.cache import RedisCache

from apps.products.models import Product, ProductParseJob
from apps.web_research.models import WebResearchRun
from apps.web_research.services import (
    WebResearchService, WebResearchTerminalSearchFailure,
    WebSearchOutcomeUncertain, enrichment_coverage, should_run_web_research,
)


cache = caches['coordination']


@shared_task(
    bind=True, max_retries=2, retry_backoff=True,
    retry_backoff_max=120, queue='part_parsing',
)
def run_web_research(self, run_id: int):
    existing = WebResearchRun.objects.filter(pk=run_id).only(
        'pk', 'product_id', 'status', 'claim_count',
    ).first()
    if existing is not None and existing.status not in {
        WebResearchRun.Status.QUEUED,
        WebResearchRun.Status.RUNNING,
    }:
        from apps.web_research.models import WebSearchAttempt, WebSearchWorkflow
        has_replayable_workflow = WebSearchWorkflow.objects.filter(
            run_id=run_id,
            operation='web_research',
        ).filter(
            Q(status__in=[
                WebSearchWorkflow.Status.IN_PROGRESS,
                WebSearchWorkflow.Status.APPLY_PENDING,
            ])
            | Q(
                status=WebSearchWorkflow.Status.APPLIED,
                attempts__status__in=[
                    WebSearchAttempt.Status.SUCCESS,
                    WebSearchAttempt.Status.EMPTY,
                ],
                attempts__checkpoint_enc__isnull=False,
            )
        ).exists()
        if not has_replayable_workflow:
            return {
                'run_id': existing.pk,
                'product_id': existing.product_id,
                'status': existing.status,
                'claim_count': existing.claim_count,
            }
    if not isinstance(cache, RedisCache):
        raise RuntimeError('Web research coordination cache must be RedisCache.')
    lock = cache.lock(f'lock:web_research:{run_id}', timeout=300)
    if not lock.acquire(blocking=False):
        from apps.core.dispatch import SafeRetryableDispatchError
        raise SafeRetryableDispatchError(
            'Web-research workflow is already owned by another worker.',
        )
    try:
        run = WebResearchService.execute(run_id)
        return {
            'run_id': run.pk,
            'product_id': run.product_id,
            'status': run.status,
            'claim_count': run.claim_count,
        }
    except WebSearchOutcomeUncertain:
        # The paid search may have succeeded remotely.  Do not start another
        # paid effect (AI generation) until an operator resolves that evidence.
        raise
    except WebResearchTerminalSearchFailure:
        # The complete immutable plan ended in documented safe failures and
        # its exact evidence was already acknowledged with a FAILED run. This
        # is a processed terminal outcome, not an infrastructure retry.
        failed_run = WebResearchRun.objects.get(pk=run_id)
        return {
            'run_id': failed_run.pk,
            'product_id': failed_run.product_id,
            'status': failed_run.status,
            'claim_count': failed_run.claim_count,
        }
    except Exception as exc:
        retry_run = WebResearchRun.objects.filter(pk=run_id).first()
        if (
            retry_run is not None
            and retry_run.status == WebResearchRun.Status.RUNNING
        ):
            retry_run.status = WebResearchRun.Status.QUEUED
            retry_run.finished_at = None
            retry_run.save(update_fields=[
                'status', 'finished_at', 'updated_at',
            ])
        # This task is executed through BackgroundJobDispatch. Let its durable
        # row own retries instead of asking Celery to create an untracked
        # nested delivery.
        from apps.core.dispatch import SafeRetryableDispatchError
        raise SafeRetryableDispatchError(str(exc)[:2000]) from exc
    finally:
        try:
            lock.release()
        except Exception:
            pass


@shared_task(
    bind=True, max_retries=12, default_retry_delay=5, queue='part_parsing',
)
def schedule_web_research_fallback(
    self, product_id: int, generate_after: bool = False, origin_key: str = '',
):
    try:
        product = Product.objects.select_related('tenant', 'catalog_category').get(pk=product_id)
    except Product.DoesNotExist:
        return {'product_id': product_id, 'status': 'product_not_found'}

    normalized_origin = str(origin_key).strip()
    if normalized_origin:
        # All sources spawned by one canonical parse intent share this durable
        # origin. Do not race the expensive fallback against a slow Euroauto
        # (or future) sibling, regardless of how old that job is.
        sibling_jobs = ProductParseJob.objects.filter(
            tenant=product.tenant,
            fallback_origin_key=normalized_origin,
        )
        if sibling_jobs.filter(
            status__in=[
                ProductParseJob.Status.PENDING,
                ProductParseJob.Status.RUNNING,
            ],
        ).exists():
            from apps.core.dispatch import SafeRetryableDispatchError
            raise SafeRetryableDispatchError(
                'Product parse siblings are not terminal yet.',
            )
    else:
        # Legacy callbacks have no durable family identity. Fail closed while
        # any same-product parse can still mutate coverage; never infer a
        # source list or time window that newer integrations can bypass.
        if product.parse_jobs.filter(
            fallback_origin_key='',
            status__in=[
                ProductParseJob.Status.PENDING,
                ProductParseJob.Status.RUNNING,
            ],
        ).exists():
            from apps.core.dispatch import SafeRetryableDispatchError
            raise SafeRetryableDispatchError(
                'Legacy product parse siblings are not terminal yet.',
            )

    if not should_run_web_research(product):
        if generate_after:
            from apps.core.dispatch import enqueue_durable_task
            enqueue_durable_task(
                'apps.ai_agent.tasks.generate_description_task',
                args=[product_id],
                deduplication_key=(
                    f'{origin_key}:ai-description' if origin_key else None
                ),
                max_run_attempts=4,
            )
        return {
            'product_id': product_id,
            'status': WebResearchRun.Status.SKIPPED,
            'coverage': enrichment_coverage(product),
        }

    with transaction.atomic():
        run, created = WebResearchService.create_run(
            product,
            trigger=WebResearchRun.Trigger.PARSER_FALLBACK,
            generate_after=generate_after,
            origin_key=origin_key,
        )
        if created or run.status in {
            WebResearchRun.Status.QUEUED,
            WebResearchRun.Status.RUNNING,
        }:
            # Recover a canonical queued run whose original worker died before
            # persisting delivery. The durable dedupe key makes normal replays
            # a no-op and creates the missing dispatch when required.
            enqueue_web_research_run(run.pk)
    return {'run_id': run.pk, 'product_id': product_id, 'status': run.status}


def enqueue_web_research_run(
    run_id: int,
    *,
    revive_failed: bool = False,
):
    """Persist one research execution beside its domain record."""
    from apps.core.dispatch import enqueue_durable_task
    return enqueue_durable_task(
        'apps.web_research.tasks.run_web_research',
        args=[run_id],
        deduplication_key=f'web-research-run:{run_id}',
        max_run_attempts=3,
        revive_failed=revive_failed,
    )
