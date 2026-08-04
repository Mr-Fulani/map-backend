"""Celery tasks for API-backed product media processing."""

from celery import shared_task


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

    if job.status in (
        MediaProcessingJob.Status.SUCCEEDED,
        MediaProcessingJob.Status.CANCELLED,
    ):
        return {'job_id': job.pk, 'status': job.status}

    try:
        job = submit_job(job)
    except Exception as exc:
        fail_job(job, 'submission_failed', str(exc))
        raise self.retry(exc=exc)
    return {'job_id': job.pk, 'status': job.status, 'provider_id': job.provider_id}
