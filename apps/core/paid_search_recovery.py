"""Guarded local-apply recovery for durable paid-search checkpoints."""

from django.db import transaction

from apps.core.advisory_lock import try_session_advisory_lock
from apps.core.models import BackgroundJobDispatch
from apps.tenants.models import Tenant
from apps.web_research.models import WebSearchAttempt, WebSearchWorkflow


class PaidSearchCheckpointRecoveryError(RuntimeError):
    """The requested owner is not proven safe for provider-free replay."""


def _assert_safe_local_replay(
    workflow: WebSearchWorkflow,
    attempts: list[WebSearchAttempt],
) -> None:
    """Accept only known checkpoints or proven pre-send/safe failures."""
    if workflow.status not in {
        WebSearchWorkflow.Status.IN_PROGRESS,
        WebSearchWorkflow.Status.APPLY_PENDING,
    }:
        raise PaidSearchCheckpointRecoveryError(
            'Paid-search workflow is not waiting for safe local replay.',
        )

    saw_checkpoint = False
    for attempt in attempts:
        if (
            attempt.reconciliation_state
            == WebSearchAttempt.ReconciliationState.PENDING
            or attempt.status in {
                WebSearchAttempt.Status.STARTED,
                WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
            }
        ):
            raise PaidSearchCheckpointRecoveryError(
                'Paid-search workflow contains an uncertain provider outcome.',
            )
        if attempt.status in {
            WebSearchAttempt.Status.SUCCESS,
            WebSearchAttempt.Status.EMPTY,
        }:
            if (
                attempt.apply_state != WebSearchAttempt.ApplyState.PENDING
                or not attempt.checkpoint_enc
            ):
                raise PaidSearchCheckpointRecoveryError(
                    'Paid-search result has no replayable checkpoint.',
                )
            saw_checkpoint = True
            continue
        if (
            attempt.status
            not in {
                WebSearchAttempt.Status.FAILED,
                WebSearchAttempt.Status.SKIPPED,
            }
            or attempt.reconciliation_state
            != WebSearchAttempt.ReconciliationState.NOT_REQUIRED
            or attempt.apply_state != WebSearchAttempt.ApplyState.PENDING
        ):
            raise PaidSearchCheckpointRecoveryError(
                'Paid-search attempt is not safe for automatic replay.',
            )

    if (
        workflow.status == WebSearchWorkflow.Status.APPLY_PENDING
        and not saw_checkpoint
    ):
        raise PaidSearchCheckpointRecoveryError(
            'Apply-pending workflow has no known provider checkpoint.',
        )


def _lock_safe_workflow(
    *, tenant_id: int, operation: str, workflow_key: str,
) -> WebSearchWorkflow:
    workflow = WebSearchWorkflow.objects.select_for_update().filter(
        tenant_id=tenant_id,
        operation=operation,
        workflow_key=workflow_key,
    ).first()
    if workflow is None:
        raise PaidSearchCheckpointRecoveryError(
            'Canonical paid-search workflow was not found.',
        )
    attempts = list(workflow.attempts.select_for_update().order_by('pk'))
    _assert_safe_local_replay(workflow, attempts)
    return workflow


def _revive_exact_dispatch(
    dispatch: BackgroundJobDispatch,
) -> BackgroundJobDispatch:
    if (
        dispatch.status != BackgroundJobDispatch.Status.FAILED
        or not dispatch.deduplication_key
    ):
        raise PaidSearchCheckpointRecoveryError(
            'Canonical dispatch is not an exhausted recoverable delivery.',
        )
    from apps.core.dispatch import enqueue_durable_task
    return enqueue_durable_task(
        dispatch.task_name,
        args=dispatch.args,
        kwargs=dispatch.kwargs,
        deduplication_key=dispatch.deduplication_key,
        max_run_attempts=dispatch.max_run_attempts,
        execution_timeout_seconds=dispatch.execution_timeout_seconds,
        revive_failed=True,
    )


def resume_image_search_checkpoint(task_id: int) -> BackgroundJobDispatch:
    """Revive one exact failed image owner without another provider call."""
    from apps.image_search.models import ImageSearchTask

    workflow_key = f'image-search-task:{int(task_id)}'
    with try_session_advisory_lock(f'image-search:{workflow_key}') as acquired:
        if not acquired:
            raise PaidSearchCheckpointRecoveryError(
                'Image-search workflow is currently owned by another worker.',
            )
        with transaction.atomic():
            try:
                tenant_id = ImageSearchTask.objects.values_list(
                    'tenant_id', flat=True,
                ).get(pk=task_id)
            except ImageSearchTask.DoesNotExist as exc:
                raise PaidSearchCheckpointRecoveryError(
                    'Image-search task was not found.',
                ) from exc
            Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
            tracking = ImageSearchTask.objects.select_for_update().get(
                pk=task_id,
                tenant_id=tenant_id,
            )
            if tracking.status != ImageSearchTask.Status.FAILED:
                raise PaidSearchCheckpointRecoveryError(
                    'Image-search task is not an exhausted failed owner.',
                )
            _lock_safe_workflow(
                tenant_id=tenant_id,
                operation='image_search',
                workflow_key=workflow_key,
            )
            if tracking.dispatch_id is None:
                raise PaidSearchCheckpointRecoveryError(
                    'Image-search task has no canonical dispatch.',
                )
            dispatch = BackgroundJobDispatch.objects.select_for_update().filter(
                pk=tracking.dispatch_id,
            ).first()
            if (
                dispatch is None
                or dispatch.task_name
                != 'apps.image_search.tasks.search_images_for_product'
                or dispatch.args != [tracking.product_id, tracking.pk]
                or dispatch.kwargs
                or dispatch.deduplication_key
                != f'image-search-request:{tracking.task_id}'
            ):
                raise PaidSearchCheckpointRecoveryError(
                    'Image-search dispatch does not match its durable owner.',
                )
            tracking.status = ImageSearchTask.Status.PENDING
            tracking.result = None
            tracking.error_code = ''
            tracking.error_message = ''
            tracking.finished_at = None
            tracking.save(update_fields=[
                'status', 'result', 'error_code', 'error_message',
                'finished_at', 'updated_at',
            ])
            return _revive_exact_dispatch(dispatch)


def resume_euroauto_checkpoint(job_id: int) -> BackgroundJobDispatch:
    """Revive one exact failed Euroauto owner without another provider call."""
    from apps.products.models import ProductParseJob

    workflow_key = f'product-parse-job:{int(job_id)}'
    with try_session_advisory_lock(workflow_key) as acquired:
        if not acquired:
            raise PaidSearchCheckpointRecoveryError(
                'Euroauto workflow is currently owned by another worker.',
            )
        with transaction.atomic():
            try:
                tenant_id = ProductParseJob.objects.values_list(
                    'tenant_id', flat=True,
                ).get(pk=job_id)
            except ProductParseJob.DoesNotExist as exc:
                raise PaidSearchCheckpointRecoveryError(
                    'Euroauto parse job was not found.',
                ) from exc
            Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
            job = ProductParseJob.objects.select_for_update().get(
                pk=job_id,
                tenant_id=tenant_id,
            )
            if (
                job.source_id != 'euroauto'
                or job.status != ProductParseJob.Status.FAILED
                or job.product_id is None
            ):
                raise PaidSearchCheckpointRecoveryError(
                    'Parse job is not an exhausted Euroauto workflow owner.',
                )
            _lock_safe_workflow(
                tenant_id=tenant_id,
                operation='euroauto',
                workflow_key=workflow_key,
            )
            dispatches = list(
                BackgroundJobDispatch.objects.select_for_update().filter(
                    task_name__in=[
                        'apps.products.tasks.parse_single_part',
                        (
                            'apps.products.tasks.'
                            'parse_single_part_then_generate_description'
                        ),
                    ],
                    args=[job.pk],
                )[:2]
            )
            if len(dispatches) != 1 or dispatches[0].kwargs:
                raise PaidSearchCheckpointRecoveryError(
                    'Exact canonical Euroauto dispatch was not found.',
                )
            dispatch = dispatches[0]
            allowed_keys = {
                f'product-parse-job:{job.pk}',
                f'product-parse-job:{job.pk}:generate-after',
            }
            if job.fallback_origin_key:
                allowed_keys.add(job.fallback_origin_key)
            if dispatch.deduplication_key not in allowed_keys:
                raise PaidSearchCheckpointRecoveryError(
                    'Euroauto dispatch does not match its durable owner.',
                )
            job.status = ProductParseJob.Status.PENDING
            job.error_message = ''
            job.started_at = None
            job.finished_at = None
            job.save(update_fields=[
                'status', 'error_message', 'started_at', 'finished_at',
                'updated_at',
            ])
            return _revive_exact_dispatch(dispatch)
