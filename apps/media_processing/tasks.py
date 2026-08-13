"""Celery tasks for API-backed product media processing."""

from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils.timezone import now


@shared_task(bind=True, max_retries=3, retry_backoff=True, queue='media_processing')
def process_media_job(self, job_id: int) -> dict:
    from apps.media_processing.models import MediaProcessingJob
    from apps.core.dispatch import SafeRetryableDispatchError
    from apps.media_processing.services import (
        MediaProviderCheckpointApplyInProgress,
        MediaProviderOutcomeUncertain,
        fail_job,
        fail_job_if_checkpoint_unresolved,
        submit_job,
    )

    try:
        job = MediaProcessingJob.objects.select_related(
            'tenant', 'product_image__product', 'preset',
        ).get(pk=job_id)
    except MediaProcessingJob.DoesNotExist:
        return {'job_id': job_id, 'status': 'missing'}

    claimable = (
        Q(status=MediaProcessingJob.Status.QUEUED)
        | Q(
            status=MediaProcessingJob.Status.FAILED,
            error_code='submission_failed',
        )
        | Q(
            provider_response_state=(
                MediaProcessingJob.ProviderResponseState.RECORDED
            ),
        )
        | Q(
            provider_response_state=(
                MediaProcessingJob.ProviderResponseState.APPLYING
            ),
        )
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
    except MediaProviderCheckpointApplyInProgress:
        return {
            'job_id': job.pk,
            'status': MediaProcessingJob.ProviderResponseState.APPLYING,
        }
    except MediaProviderOutcomeUncertain as exc:
        message = (
            'Результат провайдера не применён автоматически; требуется сверка.'
        )
        if not fail_job_if_checkpoint_unresolved(
            job, 'outcome_uncertain', message,
        ):
            return {
                'job_id': job.pk,
                'status': job.status,
                'provider_id': job.provider_id,
            }
        raise RuntimeError(message) from exc
    except Exception as exc:
        message = 'Не удалось безопасно отправить задачу медиа-провайдеру.'
        fail_job(job, 'submission_failed', message)
        raise SafeRetryableDispatchError(message) from exc
    return {'job_id': job.pk, 'status': job.status, 'provider_id': job.provider_id}


@shared_task(queue='media_processing')
def resume_stale_media_provider_checkpoints() -> dict:
    """Durably enqueue stale local apply claims without provider resubmission."""
    from apps.core.dispatch import enqueue_durable_task
    from apps.media_processing.models import MediaProcessingJob

    cutoff = now() - timedelta(minutes=10)
    stale_jobs = list(
        MediaProcessingJob.objects.filter(
            provider_response_state=(
                MediaProcessingJob.ProviderResponseState.APPLYING
            ),
        ).filter(
            Q(provider_response_apply_claimed_at__lt=cutoff)
            | Q(provider_response_apply_claimed_at__isnull=True)
        ).order_by('pk').values(
            'pk', 'provider_response_digest', 'provider_response_apply_token',
        )[:100],
    )
    for stale_job in stale_jobs:
        enqueue_durable_task(
            'apps.media_processing.tasks.process_media_job',
            args=[stale_job['pk']],
            deduplication_key=(
                f'media-checkpoint-resume:{stale_job["pk"]}:'
                f'{stale_job["provider_response_digest"]}:'
                f'{stale_job["provider_response_apply_token"] or "unclaimed"}'
            ),
            max_run_attempts=4,
            revive_failed=True,
        )
    return {'stale_checkpoints_enqueued': len(stale_jobs)}
