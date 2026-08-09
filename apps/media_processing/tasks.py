"""Celery tasks for API-backed product media processing."""

from celery import shared_task
from django.db.models import Q
from django.utils.timezone import now


@shared_task(bind=True, max_retries=3, retry_backoff=True, queue='media_processing')
def process_media_job(self, job_id: int) -> dict:
    from apps.media_processing.models import MediaProcessingJob
    from apps.media_processing.services import fail_job, submit_job

    try:
        job = MediaProcessingJob.objects.select_related(
            'tenant', 'product_image__product', 'preset',
        ).get(pk=job_id)
    except MediaProcessingJob.DoesNotExist:
        return {'job_id': job_id, 'status': 'missing'}

    claimable = Q(status=MediaProcessingJob.Status.QUEUED)
    if self.request.retries > 0:
        claimable |= Q(
            status=MediaProcessingJob.Status.FAILED,
            error_code='submission_failed',
        )
    claimed_at = now()
    claimed = MediaProcessingJob.objects.filter(
        pk=job.pk,
    ).filter(
        claimable,
    ).update(
        status=MediaProcessingJob.Status.PROCESSING,
        started_at=claimed_at,
        error_code='',
        error_message='',
        updated_at=claimed_at,
    )
    if not claimed:
        job.refresh_from_db(fields=['status'])
        return {'job_id': job.pk, 'status': job.status}
    job.refresh_from_db()

    try:
        job = submit_job(job)
    except Exception as exc:
        fail_job(job, 'submission_failed', str(exc))
        raise self.retry(exc=exc)
    return {'job_id': job.pk, 'status': job.status, 'provider_id': job.provider_id}
