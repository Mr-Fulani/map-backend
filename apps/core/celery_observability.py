"""Celery publish timestamps and low-cardinality lifecycle metrics."""

import threading
import time
from collections import OrderedDict
from typing import Any

from celery.signals import (
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_revoked,
    task_success,
)

from apps.core.telemetry import metric_count, metric_distribution


ENQUEUED_AT_HEADER = 'map_enqueued_at_ms'
FIRST_PUBLISHED_AT_HEADER = 'map_first_published_at_ms'
_MAX_ACTIVE_TIMERS = 4096
_active_timers: OrderedDict[str, float] = OrderedDict()
_timer_lock = threading.Lock()

_TASK_FAMILY_PREFIXES = (
    ('apps.ai_agent.', 'ai'),
    ('apps.billing.', 'billing'),
    ('apps.core.', 'core'),
    ('apps.image_search.', 'image_search'),
    ('apps.marketplaces.', 'marketplace'),
    ('apps.media_processing.', 'media'),
    ('apps.notifications.', 'notifications'),
    ('apps.products.', 'products'),
    ('apps.sync.', 'sync'),
    ('apps.tenants.', 'tenants'),
    ('apps.users.', 'notifications'),
    ('apps.web_research.', 'web_research'),
)


def _task_family(task: Any) -> str:
    task_name = str(getattr(task, 'name', '') or '')
    for prefix, family in _TASK_FAMILY_PREFIXES:
        if task_name.startswith(prefix):
            return family
    return 'other'


def _task_queue(task: Any = None, request: Any = None) -> str:
    task_request = request or getattr(task, 'request', None)
    delivery_info = getattr(task_request, 'delivery_info', None) or {}
    return str(delivery_info.get('routing_key') or 'other')


def _attributes(task: Any, outcome: str, request: Any = None) -> dict[str, str]:
    return {
        'task_family': _task_family(task),
        'queue': _task_queue(task, request),
        'outcome': outcome,
    }


def stamp_task_publish(headers=None, **_kwargs) -> None:
    """Stamp every broker publication; preserve the first logical publish time."""
    if not isinstance(headers, dict):
        return
    published_at_ms = int(time.time() * 1000)
    headers[ENQUEUED_AT_HEADER] = published_at_ms
    headers.setdefault(FIRST_PUBLISHED_AT_HEADER, published_at_ms)


def observe_task_prerun(task_id=None, **_kwargs) -> None:
    if not task_id:
        return
    with _timer_lock:
        _active_timers[str(task_id)] = time.monotonic()
        _active_timers.move_to_end(str(task_id))
        while len(_active_timers) > _MAX_ACTIVE_TIMERS:
            _active_timers.popitem(last=False)


def observe_task_postrun(task_id=None, task=None, state=None, **_kwargs) -> None:
    started_at = None
    if task_id:
        with _timer_lock:
            started_at = _active_timers.pop(str(task_id), None)
    if started_at is None:
        return
    outcome = {
        'SUCCESS': 'success',
        'FAILURE': 'failure',
        'RETRY': 'retry',
        'REVOKED': 'revoked',
    }.get(str(state or '').upper(), 'other')
    metric_distribution(
        'map.celery.task.runtime',
        max(0.0, time.monotonic() - started_at),
        unit='second',
        attributes=_attributes(task, outcome),
    )


def observe_task_success(sender=None, **_kwargs) -> None:
    metric_count(
        'map.celery.task.execution',
        attributes=_attributes(sender, 'success'),
    )


def observe_task_failure(sender=None, **_kwargs) -> None:
    metric_count(
        'map.celery.task.execution',
        attributes=_attributes(sender, 'failure'),
    )


def observe_task_retry(sender=None, request=None, **_kwargs) -> None:
    metric_count(
        'map.celery.task.execution',
        attributes=_attributes(sender, 'retry', request),
    )


def observe_task_revoked(sender=None, request=None, **_kwargs) -> None:
    metric_count(
        'map.celery.task.execution',
        attributes=_attributes(sender, 'revoked', request),
    )


before_task_publish.connect(
    stamp_task_publish,
    weak=False,
    dispatch_uid='map.celery_observability.publish_stamp.v1',
)
task_prerun.connect(
    observe_task_prerun,
    weak=False,
    dispatch_uid='map.celery_observability.prerun.v1',
)
task_postrun.connect(
    observe_task_postrun,
    weak=False,
    dispatch_uid='map.celery_observability.postrun.v1',
)
task_success.connect(
    observe_task_success,
    weak=False,
    dispatch_uid='map.celery_observability.success.v1',
)
task_failure.connect(
    observe_task_failure,
    weak=False,
    dispatch_uid='map.celery_observability.failure.v1',
)
task_retry.connect(
    observe_task_retry,
    weak=False,
    dispatch_uid='map.celery_observability.retry.v1',
)
task_revoked.connect(
    observe_task_revoked,
    weak=False,
    dispatch_uid='map.celery_observability.revoked.v1',
)
