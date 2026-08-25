import time

from celery import shared_task
from sentry_sdk.crons import MonitorStatus, capture_checkin
from sentry_sdk.types import MonitorConfig


SENTRY_COLLECTOR_MONITOR_SLUG = 'map-celery-observability-collector'
SENTRY_COLLECTOR_MONITOR_CONFIG: MonitorConfig = {
    'schedule': {
        'type': 'interval',
        'value': 1,
        'unit': 'minute',
    },
    'checkin_margin': 1,
    'max_runtime': 1,
    'failure_issue_threshold': 1,
    'recovery_threshold': 1,
}


def _capture_collector_check_in(
    *,
    status: str,
    check_in_id: str | None = None,
    duration: float | None = None,
) -> str | None:
    """Emit the collector dead-man check-in without affecting task outcome."""
    try:
        return capture_checkin(
            monitor_slug=SENTRY_COLLECTOR_MONITOR_SLUG,
            check_in_id=check_in_id,
            status=status,
            duration=duration,
            monitor_config=SENTRY_COLLECTOR_MONITOR_CONFIG,
        )
    except Exception:
        return check_in_id


def _collect_celery_observability_snapshot():
    from apps.core.queue_observability import (
        cache_celery_queue_snapshot,
        collect_celery_queue_snapshot,
        emit_celery_queue_snapshot_metrics,
    )

    snapshot = collect_celery_queue_snapshot()
    snapshot['cache_status'] = 'ok'
    try:
        cache_celery_queue_snapshot(snapshot)
    except Exception:
        snapshot['cache_status'] = 'unavailable'
        snapshot['collector_status'] = (
            'unavailable'
            if snapshot['collector_status'] == 'unavailable'
            else 'degraded'
        )
    emit_celery_queue_snapshot_metrics(snapshot)
    return snapshot


@shared_task(
    queue='notifications',
    expires=50,
    soft_time_limit=12,
    time_limit=15,
    ignore_result=True,
)
def collect_celery_observability():
    """Publish/cache one bounded broker and worker snapshot every minute."""
    started_at = time.monotonic()
    check_in_id = _capture_collector_check_in(
        status=MonitorStatus.IN_PROGRESS,
    )
    try:
        snapshot = _collect_celery_observability_snapshot()
    except BaseException:
        _capture_collector_check_in(
            check_in_id=check_in_id,
            status=MonitorStatus.ERROR,
            duration=time.monotonic() - started_at,
        )
        raise
    _capture_collector_check_in(
        check_in_id=check_in_id,
        status=MonitorStatus.OK,
        duration=time.monotonic() - started_at,
    )
    return snapshot


@shared_task(queue='notifications')
def purge_retained_data_task():
    from apps.core.retention import purge_retained_data
    return purge_retained_data()


@shared_task(
    queue='notifications',
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def dispatch_due_background_jobs(limit: int = 200):
    """Recover pending deliveries and expired publisher/worker leases."""
    from apps.core.dispatch import (
        publish_due_dispatches,
        recover_terminal_feed_intent_dispatches,
    )
    feed_intent_recovery = recover_terminal_feed_intent_dispatches(limit=limit)
    result = publish_due_dispatches(limit=limit)
    result['feed_intent_recovery'] = feed_intent_recovery
    return result


@shared_task(
    bind=True,
    name='apps.core.tasks.execute_background_dispatch',
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def execute_background_dispatch(self, dispatch_id: str, claim_token: str):
    """Claim one durable delivery before invoking its allowlisted target task."""
    from apps.core.dispatch import claim_dispatch, execute_claimed_dispatch
    dispatch = claim_dispatch(dispatch_id, claim_token)
    if dispatch is None:
        return {'dispatch_id': dispatch_id, 'status': 'duplicate_or_stale'}
    return execute_claimed_dispatch(dispatch)
