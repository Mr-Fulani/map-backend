"""Durable delivery for user-visible background jobs.

The database is the source of truth and Celery/Redis is only the transport.
Only explicitly allowlisted task names can be executed from persisted rows.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import uuid
from typing import Any

from celery import current_app
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils.timezone import now

from apps.core.models import BackgroundJobDispatch


logger = logging.getLogger(__name__)


class SafeRetryableDispatchError(RuntimeError):
    """Failure proven to have happened before crossing an external boundary."""


DURABLE_TASK_QUEUES = {
    'apps.products.tasks.parse_single_part': 'part_parsing',
    'apps.products.tasks.parse_single_part_then_generate_description': 'part_parsing',
    'apps.products.tasks.process_bulk_product_action': 'part_parsing_bulk',
    'apps.ai_agent.tasks.generate_description_task': 'ai_generate',
    'apps.image_search.tasks.search_images_for_product': 'image_search',
    'apps.media_processing.tasks.process_media_job': 'media_processing',
    'apps.marketplaces.tasks.process_marketplace_feed_run_step': 'avito_publish',
    'apps.web_research.tasks.schedule_web_research_fallback': 'part_parsing',
    'apps.web_research.tasks.run_web_research': 'part_parsing',
}

_LEASED_STATES = {
    BackgroundJobDispatch.Status.PUBLISHING,
    BackgroundJobDispatch.Status.PUBLISHED,
    BackgroundJobDispatch.Status.RUNNING,
}
_TERMINAL_STATES = {
    BackgroundJobDispatch.Status.SUCCEEDED,
    BackgroundJobDispatch.Status.FAILED,
    BackgroundJobDispatch.Status.CANCELLED,
}

_OUTCOME_UNCERTAIN_RESULT = {
    'reason_code': 'outcome_uncertain',
    'message': (
        'Результат внешнего провайдера неизвестен; '
        'автоматический повтор запрещён.'
    ),
}

# These targets can cross a provider boundary that does not offer a proven
# exactly-once contract.  Once execution starts, a lost worker leaves the
# provider outcome uncertain: replaying could charge a tenant twice or submit
# the same media operation twice.  They therefore fail closed for operator/user
# reconciliation instead of being automatically replayed.
_NO_AUTOMATIC_REPLAY_TASKS = {
    'apps.ai_agent.tasks.generate_description_task',
    'apps.media_processing.tasks.process_media_job',
}


def enqueue_durable_task(
    task_name: str,
    *,
    args: list | tuple | None = None,
    kwargs: dict | None = None,
    deduplication_key: str | None = None,
    available_at=None,
    max_run_attempts: int = 5,
    execution_timeout_seconds: int = 3700,
    revive_failed: bool = False,
) -> BackgroundJobDispatch:
    """Persist a task in the caller's transaction and kick delivery after commit.

    The after-commit callback is only a latency optimisation.  A failed callback
    leaves the row pending and the periodic dispatcher will publish it later.
    """
    try:
        queue = DURABLE_TASK_QUEUES[task_name]
    except KeyError as exc:
        raise ValueError(f'Task is not allowed for durable dispatch: {task_name}') from exc

    defaults = {
        'task_name': task_name,
        'queue': queue,
        'args': list(args or []),
        'kwargs': dict(kwargs or {}),
        'available_at': available_at or now(),
        'max_run_attempts': max(1, min(int(max_run_attempts), 25)),
        'execution_timeout_seconds': max(60, min(int(execution_timeout_seconds), 7200)),
    }
    if deduplication_key:
        dispatch, created = BackgroundJobDispatch.objects.get_or_create(
            deduplication_key=deduplication_key,
            defaults=defaults,
        )
        if (
            not created
            and revive_failed
            and dispatch.status == BackgroundJobDispatch.Status.FAILED
        ):
            dispatch.status = BackgroundJobDispatch.Status.PENDING
            dispatch.available_at = available_at or now()
            dispatch.claim_token = None
            dispatch.lease_expires_at = None
            dispatch.celery_task_id = None
            dispatch.run_attempts = 0
            dispatch.last_error = ''
            dispatch.result = None
            dispatch.started_at = None
            dispatch.finished_at = None
            dispatch.save(update_fields=[
                'status', 'available_at', 'claim_token', 'lease_expires_at',
                'celery_task_id', 'run_attempts', 'last_error', 'result',
                'started_at', 'finished_at', 'updated_at',
            ])
    else:
        dispatch = BackgroundJobDispatch.objects.create(**defaults)

    dispatch_id = dispatch.pk
    transaction.on_commit(lambda: publish_dispatch(dispatch_id))
    return dispatch


def publish_dispatch(dispatch_id) -> bool:
    """Claim and publish one due row. Safe under concurrent dispatchers."""
    publish_time = now()
    with transaction.atomic():
        try:
            dispatch = BackgroundJobDispatch.objects.select_for_update().get(pk=dispatch_id)
        except BackgroundJobDispatch.DoesNotExist:
            return False
        if dispatch.status in _TERMINAL_STATES:
            return False
        if dispatch.status == BackgroundJobDispatch.Status.PENDING:
            if dispatch.available_at > publish_time:
                return False
        elif dispatch.status in _LEASED_STATES:
            if dispatch.lease_expires_at is None or dispatch.lease_expires_at > publish_time:
                return False
            if (
                dispatch.status == BackgroundJobDispatch.Status.RUNNING
                and dispatch.task_name in _NO_AUTOMATIC_REPLAY_TASKS
            ):
                error = (
                    'Результат внешней операции после потери worker неизвестен; '
                    'автоматический повтор запрещён во избежание двойного списания '
                    'или повторной отправки.'
                )
                dispatch.status = BackgroundJobDispatch.Status.FAILED
                dispatch.finished_at = publish_time
                dispatch.last_error = error
                dispatch.result = _OUTCOME_UNCERTAIN_RESULT
                dispatch.claim_token = None
                dispatch.celery_task_id = None
                dispatch.lease_expires_at = None
                dispatch.save(update_fields=[
                    'status', 'finished_at', 'last_error', 'claim_token',
                    'result', 'celery_task_id', 'lease_expires_at', 'updated_at',
                ])
                _mark_terminal_domain_failure(dispatch, error, uncertain=True)
                logger.error(
                    'Durable external-effect task has an uncertain outcome: dispatch=%s',
                    dispatch_id,
                )
                return False
        else:
            return False

        claim_token = uuid.uuid4()
        celery_task_id = uuid.uuid4()
        dispatch.status = BackgroundJobDispatch.Status.PUBLISHING
        dispatch.claim_token = claim_token
        dispatch.celery_task_id = celery_task_id
        dispatch.lease_expires_at = publish_time + timedelta(seconds=90)
        dispatch.publish_attempts += 1
        dispatch.save(update_fields=[
            'status', 'claim_token', 'celery_task_id', 'lease_expires_at',
            'publish_attempts', 'updated_at',
        ])
        queue = dispatch.queue
        publish_attempt = dispatch.publish_attempts

    try:
        from apps.core.tasks import execute_background_dispatch
        execute_background_dispatch.apply_async(
            args=[str(dispatch_id), str(claim_token)],
            queue=queue,
            task_id=str(celery_task_id),
        )
    except Exception as exc:
        retry_at = now() + timedelta(seconds=_retry_delay(publish_attempt))
        BackgroundJobDispatch.objects.filter(
            pk=dispatch_id,
            status=BackgroundJobDispatch.Status.PUBLISHING,
            claim_token=claim_token,
        ).update(
            status=BackgroundJobDispatch.Status.PENDING,
            available_at=retry_at,
            claim_token=None,
            celery_task_id=None,
            lease_expires_at=None,
            last_error=_safe_error(exc),
            updated_at=now(),
        )
        logger.warning('Durable task publish failed: dispatch=%s', dispatch_id, exc_info=True)
        return False

    # A very fast worker may already have moved PUBLISHING to RUNNING.
    BackgroundJobDispatch.objects.filter(
        pk=dispatch_id,
        status=BackgroundJobDispatch.Status.PUBLISHING,
        claim_token=claim_token,
    ).update(
        status=BackgroundJobDispatch.Status.PUBLISHED,
        lease_expires_at=now() + timedelta(seconds=120),
        last_error='',
        updated_at=now(),
    )
    return True


def publish_due_dispatches(limit: int = 200) -> dict:
    """Publish pending jobs and reclaim expired publish/run leases."""
    current_time = now()
    batch_limit = max(1, min(int(limit), 1000))
    due = (
        Q(
            status=BackgroundJobDispatch.Status.PENDING,
            available_at__lte=current_time,
        )
        | Q(
            status__in=_LEASED_STATES,
            lease_expires_at__lte=current_time,
        )
    )
    dispatch_ids = list(
        BackgroundJobDispatch.objects.filter(due)
        .order_by('available_at', 'created_at')
        .values_list('pk', flat=True)[:batch_limit]
    )
    published = 0
    for dispatch_id in dispatch_ids:
        if publish_dispatch(dispatch_id):
            published += 1
    return {'selected': len(dispatch_ids), 'published': published}


def claim_dispatch(dispatch_id, claim_token) -> BackgroundJobDispatch | None:
    """Atomically claim a delivery; duplicate/stale messages become no-ops."""
    claim_time = now()
    with transaction.atomic():
        try:
            dispatch = BackgroundJobDispatch.objects.select_for_update().get(pk=dispatch_id)
        except BackgroundJobDispatch.DoesNotExist:
            return None
        if dispatch.status in _TERMINAL_STATES:
            return None
        if dispatch.claim_token != uuid.UUID(str(claim_token)):
            return None
        if dispatch.status not in {
            BackgroundJobDispatch.Status.PUBLISHING,
            BackgroundJobDispatch.Status.PUBLISHED,
        }:
            return None
        if dispatch.run_attempts >= dispatch.max_run_attempts:
            dispatch.status = BackgroundJobDispatch.Status.FAILED
            dispatch.finished_at = claim_time
            dispatch.last_error = 'Превышено количество попыток выполнения.'
            dispatch.lease_expires_at = None
            dispatch.save(update_fields=[
                'status', 'finished_at', 'last_error', 'lease_expires_at', 'updated_at',
            ])
            _mark_terminal_domain_failure(dispatch, dispatch.last_error)
            return None

        dispatch.status = BackgroundJobDispatch.Status.RUNNING
        dispatch.run_attempts += 1
        dispatch.started_at = dispatch.started_at or claim_time
        dispatch.lease_expires_at = claim_time + timedelta(
            seconds=dispatch.execution_timeout_seconds,
        )
        dispatch.save(update_fields=[
            'status', 'run_attempts', 'started_at', 'lease_expires_at', 'updated_at',
        ])
        _prepare_recovered_domain_job(dispatch)
        return dispatch


def execute_claimed_dispatch(dispatch: BackgroundJobDispatch):
    """Execute the allowlisted target synchronously and persist its outcome."""
    if dispatch.task_name not in DURABLE_TASK_QUEUES:
        _finish_failed(dispatch, 'Задача больше не разрешена для выполнения.', terminal=True)
        return {'dispatch_id': str(dispatch.pk), 'status': 'failed'}
    task = _registered_task(dispatch.task_name)
    if task is None:
        _finish_failed(dispatch, f'Celery task не зарегистрирована: {dispatch.task_name}')
        return {'dispatch_id': str(dispatch.pk), 'status': 'retrying'}
    try:
        result = task.run(*dispatch.args, **dispatch.kwargs)
    except Exception as exc:
        safe_retry = isinstance(exc, SafeRetryableDispatchError)
        explicit_uncertain = bool(getattr(exc, 'outcome_uncertain', False))
        uncertain = (
            explicit_uncertain
            or (
                dispatch.task_name in _NO_AUTOMATIC_REPLAY_TASKS
                and not safe_retry
            )
        )
        terminal = uncertain or dispatch.run_attempts >= dispatch.max_run_attempts
        _finish_failed(
            dispatch,
            _safe_error(exc),
            terminal=terminal,
            uncertain=uncertain,
        )
        return {
            'dispatch_id': str(dispatch.pk),
            'status': 'failed' if terminal else 'retrying',
        }

    finish_time = now()
    BackgroundJobDispatch.objects.filter(
        pk=dispatch.pk,
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token=dispatch.claim_token,
    ).update(
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        result=_json_value(result),
        last_error='',
        lease_expires_at=None,
        finished_at=finish_time,
        updated_at=finish_time,
    )
    return {'dispatch_id': str(dispatch.pk), 'status': 'succeeded'}


def _finish_failed(
    dispatch: BackgroundJobDispatch,
    error: str,
    *,
    terminal: bool = False,
    uncertain: bool = False,
) -> None:
    failure_time = now()
    next_status = (
        BackgroundJobDispatch.Status.FAILED
        if terminal
        else BackgroundJobDispatch.Status.PENDING
    )
    updates: dict[str, Any] = {
        'status': next_status,
        'last_error': error,
        'claim_token': None,
        'celery_task_id': None,
        'lease_expires_at': None,
        'updated_at': failure_time,
    }
    if terminal:
        updates['finished_at'] = failure_time
        if uncertain:
            updates['result'] = _OUTCOME_UNCERTAIN_RESULT
    else:
        updates['available_at'] = failure_time + timedelta(
            seconds=_retry_delay(dispatch.run_attempts),
        )
    changed = BackgroundJobDispatch.objects.filter(
        pk=dispatch.pk,
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token=dispatch.claim_token,
    ).update(**updates)
    if not changed:
        return
    if terminal:
        _mark_terminal_domain_failure(dispatch, error, uncertain=uncertain)
    else:
        _mark_retrying_domain_job(dispatch, error)


def _mark_retrying_domain_job(dispatch: BackgroundJobDispatch, error: str) -> None:
    """Keep domain status consistent while the dispatch waits for a retry."""
    object_id = _first_int_arg(dispatch)
    if object_id is None:
        return
    if dispatch.task_name.startswith('apps.products.tasks.parse_single_part'):
        from apps.products.models import ProductParseJob
        ProductParseJob.objects.filter(
            pk=object_id,
            status__in=[ProductParseJob.Status.RUNNING, ProductParseJob.Status.FAILED],
        ).update(
            status=ProductParseJob.Status.PENDING,
            error_message=error,
            finished_at=None,
            updated_at=now(),
        )
    elif dispatch.task_name == 'apps.products.tasks.process_bulk_product_action':
        from apps.products.models import ProductBulkActionJob
        ProductBulkActionJob.objects.filter(
            pk=object_id,
            status=ProductBulkActionJob.Status.RUNNING,
        ).update(
            status=ProductBulkActionJob.Status.PENDING,
            error_message=error,
            last_dispatched_at=None,
            updated_at=now(),
        )
    elif dispatch.task_name == 'apps.media_processing.tasks.process_media_job':
        from apps.media_processing.models import MediaProcessingJob
        MediaProcessingJob.objects.filter(
            pk=object_id,
            status=MediaProcessingJob.Status.FAILED,
            error_code='submission_failed',
        ).update(
            status=MediaProcessingJob.Status.QUEUED,
            finished_at=None,
            updated_at=now(),
        )
    elif dispatch.task_name == 'apps.web_research.tasks.run_web_research':
        from apps.web_research.models import WebResearchRun
        WebResearchRun.objects.filter(
            pk=object_id,
            status=WebResearchRun.Status.RUNNING,
        ).update(
            status=WebResearchRun.Status.QUEUED,
            error_message=error,
            finished_at=None,
            updated_at=now(),
        )


def _prepare_recovered_domain_job(dispatch: BackgroundJobDispatch) -> None:
    """Reset a crash-interrupted state only after a durable lease was reclaimed."""
    if (
        dispatch.run_attempts <= 1
        or dispatch.task_name != 'apps.media_processing.tasks.process_media_job'
    ):
        return
    object_id = _first_int_arg(dispatch)
    if object_id is None:
        return
    from apps.media_processing.models import MediaProcessingJob
    MediaProcessingJob.objects.filter(
        pk=object_id,
        status=MediaProcessingJob.Status.PROCESSING,
        provider_job_id='',
    ).update(
        status=MediaProcessingJob.Status.QUEUED,
        finished_at=None,
        updated_at=now(),
    )


def _mark_terminal_domain_failure(
    dispatch: BackgroundJobDispatch,
    error: str,
    *,
    uncertain: bool = False,
) -> None:
    object_id = _first_int_arg(dispatch)
    if object_id is None:
        return
    failure_time = now()
    if dispatch.task_name.startswith('apps.products.tasks.parse_single_part'):
        from apps.products.models import ProductParseJob
        ProductParseJob.objects.filter(
            pk=object_id,
            status__in=[ProductParseJob.Status.PENDING, ProductParseJob.Status.RUNNING],
        ).update(
            status=ProductParseJob.Status.FAILED,
            error_message=error,
            finished_at=failure_time,
            updated_at=failure_time,
        )
    elif dispatch.task_name == 'apps.products.tasks.process_bulk_product_action':
        from apps.products.models import ProductBulkActionJob
        ProductBulkActionJob.objects.filter(
            pk=object_id,
            status__in=[
                ProductBulkActionJob.Status.PENDING,
                ProductBulkActionJob.Status.RUNNING,
                ProductBulkActionJob.Status.COOLING_DOWN,
            ],
        ).update(
            status=ProductBulkActionJob.Status.FAILED,
            error_message=error,
            finished_at=failure_time,
            next_batch_at=None,
            last_dispatched_at=None,
            updated_at=failure_time,
        )
    elif dispatch.task_name == 'apps.media_processing.tasks.process_media_job':
        from apps.media_processing.models import MediaProcessingJob
        unresolved_media_job = MediaProcessingJob.objects.filter(
            pk=object_id,
        )
        if uncertain:
            unresolved_media_job = unresolved_media_job.filter(
                status__in=[
                    MediaProcessingJob.Status.QUEUED,
                    MediaProcessingJob.Status.PROCESSING,
                    MediaProcessingJob.Status.FAILED,
                ],
            ).exclude(
                provider_response_state__in=[
                    MediaProcessingJob.ProviderResponseState.APPLIED,
                    MediaProcessingJob.ProviderResponseState.ACCOUNTING_RESOLVED,
                ],
            )
        else:
            unresolved_media_job = unresolved_media_job.filter(
                status__in=[
                    MediaProcessingJob.Status.QUEUED,
                    MediaProcessingJob.Status.PROCESSING,
                    MediaProcessingJob.Status.FAILED,
                ],
            )
        unresolved_media_job.update(
            status=MediaProcessingJob.Status.FAILED,
            error_code='outcome_uncertain' if uncertain else 'dispatch_failed',
            error_message=error,
            finished_at=failure_time,
            updated_at=failure_time,
        )
    elif dispatch.task_name == 'apps.image_search.tasks.search_images_for_product':
        from apps.image_search.models import ImageSearchTask
        tracking_id = _int_arg_at(dispatch, 1)
        if tracking_id is None:
            return
        ImageSearchTask.objects.filter(pk=tracking_id).exclude(
            status=ImageSearchTask.Status.SUCCEEDED,
        ).update(
            status=(
                ImageSearchTask.Status.RECONCILIATION_REQUIRED
                if uncertain
                else ImageSearchTask.Status.FAILED
            ),
            error_code=(
                'provider_reconciliation_required'
                if uncertain
                else 'dispatch_failed'
            ),
            error_message=error[:500],
            finished_at=failure_time,
            updated_at=failure_time,
        )
    elif dispatch.task_name == 'apps.web_research.tasks.run_web_research':
        from apps.web_research.models import WebResearchRun
        from apps.web_research.models import WebSearchAttempt, WebSearchWorkflow
        has_replayable_checkpoint = WebSearchWorkflow.objects.filter(
            run_id=object_id,
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
            | Q(
                status=WebSearchWorkflow.Status.APPLIED,
                attempts__isnull=True,
                run__status__in=[
                    WebResearchRun.Status.QUEUED,
                    WebResearchRun.Status.RUNNING,
                ],
            )
        ).exists()
        if has_replayable_checkpoint:
            # Exhausting a transport dispatch must not strand an already-paid
            # checkpoint. Keep the exact run replayable; an explicit recovery
            # request may revive the same dispatch without provider I/O.
            WebResearchRun.objects.filter(pk=object_id).update(
                status=WebResearchRun.Status.QUEUED,
                error_message=error,
                finished_at=None,
                updated_at=failure_time,
            )
            return
        WebResearchRun.objects.filter(
            pk=object_id,
            status__in=[WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING],
        ).update(
            status=WebResearchRun.Status.FAILED,
            error_message=error,
            finished_at=failure_time,
            updated_at=failure_time,
        )


def _first_int_arg(dispatch: BackgroundJobDispatch) -> int | None:
    return _int_arg_at(dispatch, 0)


def _int_arg_at(
    dispatch: BackgroundJobDispatch,
    position: int,
) -> int | None:
    try:
        return int(dispatch.args[position])
    except (IndexError, TypeError, ValueError):
        return None


def _retry_delay(attempt: int) -> int:
    return min(300, max(2, 2 ** min(max(int(attempt), 1), 8)))


def _safe_error(exc: BaseException) -> str:
    underlying = getattr(exc, 'exc', None)
    message = str(underlying or exc or exc.__class__.__name__)
    return message[:2000]


def _json_value(value):
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
    except (TypeError, ValueError):
        return {'repr': repr(value)[:2000]}


def _registered_task(task_name: str):
    task = current_app.tasks.get(task_name)
    if task is not None:
        return task
    # Unit tests and direct management-command execution do not necessarily run
    # Celery's worker import phase. Import the allowlisted module explicitly;
    # workers normally hit the fast path above.
    module_name, _ = task_name.rsplit('.', 1)
    __import__(module_name)
    return current_app.tasks.get(task_name)
