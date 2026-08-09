from datetime import timedelta

from celery import shared_task
from django.core.cache import caches
from django.utils.timezone import now

from apps.products.models import Product, ProductParseJob
from apps.web_research.models import WebResearchRun
from apps.web_research.services import (
    WebResearchService, enrichment_coverage, should_run_web_research,
)


cache = caches['coordination']


@shared_task(
    bind=True, max_retries=2, retry_backoff=True,
    retry_backoff_max=120, queue='part_parsing',
)
def run_web_research(self, run_id: int):
    lock = cache.lock(f'lock:web_research:{run_id}', timeout=300)
    if not lock.acquire(blocking=False):
        return {'run_id': run_id, 'status': 'already_running'}
    try:
        run = WebResearchService.execute(run_id)
        return {
            'run_id': run.pk,
            'product_id': run.product_id,
            'status': run.status,
            'claim_count': run.claim_count,
        }
    except Exception as exc:
        run = WebResearchRun.objects.filter(pk=run_id).first()
        if run is not None and self.request.retries < self.max_retries:
            run.status = WebResearchRun.Status.QUEUED
            run.save(update_fields=['status', 'updated_at'])
        elif run is not None:
            WebResearchService._generate_if_unblocked(run)
        raise self.retry(exc=exc)
    finally:
        try:
            lock.release()
        except Exception:
            pass


@shared_task(
    bind=True, max_retries=12, default_retry_delay=5, queue='part_parsing',
)
def schedule_web_research_fallback(
    self, product_id: int, generate_after: bool = False,
):
    try:
        product = Product.objects.select_related('tenant', 'catalog_category').get(pk=product_id)
    except Product.DoesNotExist:
        return {'product_id': product_id, 'status': 'product_not_found'}

    recent_jobs = product.parse_jobs.filter(
        source_id__in=['tachka', 'rossko'],
        created_at__gte=now() - timedelta(minutes=10),
    )
    if recent_jobs.filter(
        status__in=[ProductParseJob.Status.PENDING, ProductParseJob.Status.RUNNING],
    ).exists():
        raise self.retry()

    if not should_run_web_research(product):
        if generate_after:
            from apps.ai_agent.tasks import generate_description_task
            generate_description_task.delay(product_id)
        return {
            'product_id': product_id,
            'status': WebResearchRun.Status.SKIPPED,
            'coverage': enrichment_coverage(product),
        }

    run, created = WebResearchService.create_run(
        product,
        trigger=WebResearchRun.Trigger.PARSER_FALLBACK,
        generate_after=generate_after,
    )
    if created:
        run_web_research.delay(run.pk)
    return {'run_id': run.pk, 'product_id': product_id, 'status': run.status}
