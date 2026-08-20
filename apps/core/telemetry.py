"""Fail-open, low-cardinality application metrics.

Sentry is the currently deployed telemetry backend.  Keeping the wrapper here
prevents domain code from leaking tenant/entity identifiers into metric labels
and gives us one place to replace the backend later.
"""

from collections.abc import Mapping
from typing import Any

from django.conf import settings
from sentry_sdk import metrics as sentry_metrics


METRIC_NAMES = frozenset({
    'map.celery.collector.broker_up',
    'map.celery.collector.cache_up',
    'map.celery.collector.heartbeat',
    'map.celery.collector.worker_inspect_up',
    'map.celery.queue.active_count',
    'map.celery.queue.age_known',
    'map.celery.queue.depth',
    'map.celery.queue.max_active_age',
    'map.celery.queue.oldest_ready_age',
    'map.celery.queue.subscribed_workers',
    'map.celery.task.execution',
    'map.celery.task.runtime',
    'map.provider.rate_limit',
    'map.provider.request',
    'map.provider.request.duration',
    'map.sync.attempt',
    'map.sync.attempt.duration',
    'map.sync.items',
})

_ATTRIBUTE_VALUES = {
    'collector_status': frozenset({'ok', 'degraded', 'unavailable'}),
    'outcome': frozenset({'success', 'failure', 'retry', 'revoked', 'skipped', 'other'}),
    'provider': frozenset({'avito', 'media', 'web_research', 'other'}),
    'rate_limit_source': frozenset({'local', 'remote', 'other'}),
    'response_class': frozenset({
        '2xx', '3xx', '4xx', '5xx', 'network_error', 'other',
    }),
    'result': frozenset({'created', 'updated', 'unchanged', 'other'}),
    'source_type': frozenset({'1c_http', '1c_xml', 'csv', 'other'}),
    'task_family': frozenset({
        'ai',
        'billing',
        'core',
        'image_search',
        'marketplace',
        'media',
        'notifications',
        'products',
        'sync',
        'tenants',
        'web_research',
        'other',
    }),
}

_OPERATION_VALUES = frozenset({
    'autoload',
    'delete',
    'feed_poll',
    'price',
    'publish',
    'stats',
    'status',
    'update',
    'other',
})


def safe_metric_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str]:
    """Return only explicitly bounded dimensions; unknown values become ``other``."""
    if not attributes:
        return {}

    result: dict[str, str] = {}
    declared_queues = frozenset(getattr(settings, 'CELERY_TASK_QUEUES', {}))
    for key, raw_value in attributes.items():
        value = str(raw_value or '').strip().lower()
        if key == 'queue':
            result[key] = value if value in declared_queues else 'other'
        elif key == 'operation':
            result[key] = value if value in _OPERATION_VALUES else 'other'
        elif key in _ATTRIBUTE_VALUES:
            allowed = _ATTRIBUTE_VALUES[key]
            result[key] = value if value in allowed else 'other'
    return result


def metric_count(
    name: str,
    value: float = 1,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    _emit('count', name, value, unit=None, attributes=attributes)


def metric_gauge(
    name: str,
    value: float,
    *,
    unit: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    _emit('gauge', name, value, unit=unit, attributes=attributes)


def metric_distribution(
    name: str,
    value: float,
    *,
    unit: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    _emit('distribution', name, value, unit=unit, attributes=attributes)


def _emit(
    kind: str,
    name: str,
    value: float,
    *,
    unit: str | None,
    attributes: Mapping[str, Any] | None,
) -> None:
    """Telemetry must never change a business request/task outcome."""
    if name not in METRIC_NAMES:
        return
    try:
        emitter = getattr(sentry_metrics, kind)
        emitter(
            name,
            float(value),
            unit=unit,
            attributes=safe_metric_attributes(attributes),
        )
    except Exception:
        # A disabled/misconfigured telemetry backend is observed by the
        # collector dead-man alert, not by breaking the product path.
        return
