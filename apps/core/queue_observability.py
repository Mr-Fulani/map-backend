"""Bounded Celery/Redis broker snapshots for staff and alert collectors.

Only exact keys for declared queues are queried.  The collector deliberately
does not use KEYS/SCAN/LRANGE or inspect.reserved(), so its cost is independent
of backlog size and its ``ready_depth`` keeps the correct broker semantics.
"""

import json
import time
from typing import Any

from celery import current_app
from celery.app.control import Inspect
from django.conf import settings
from django.core.cache import caches

from apps.core.celery_observability import ENQUEUED_AT_HEADER
from apps.core.telemetry import metric_gauge


SNAPSHOT_CACHE_KEY = 'observability:celery-queue-snapshot:v1'
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_TTL_SECONDS = 150
COLLECTOR_INTERVAL_SECONDS = 60
BROKER_IO_TIMEOUT_SECONDS = 2
CACHE_FUTURE_SKEW_SECONDS = 5
DEFAULT_PRIORITY_STEPS = (0, 3, 6, 9)
DEFAULT_PRIORITY_SEPARATOR = '\x06\x16'


def declared_queue_names() -> tuple[str, ...]:
    return tuple(sorted(getattr(settings, 'CELERY_TASK_QUEUES', {})))


def _broker_queue_keys(queue: str) -> tuple[str, ...]:
    """Return exact physical Redis keys used by the configured Kombu transport."""
    transport_options = getattr(settings, 'CELERY_BROKER_TRANSPORT_OPTIONS', {})
    prefix = str(transport_options.get('global_keyprefix') or '')
    separator = str(transport_options.get('sep') or DEFAULT_PRIORITY_SEPARATOR)
    raw_steps = transport_options.get('priority_steps', DEFAULT_PRIORITY_STEPS)
    try:
        priority_steps = tuple(int(step) for step in raw_steps)
    except (TypeError, ValueError):
        priority_steps = DEFAULT_PRIORITY_STEPS
    return tuple(
        f'{prefix}{queue}{separator}{priority}' if priority else f'{prefix}{queue}'
        for priority in priority_steps
    )


def _redis_client():
    """Create a short-lived raw client so every collector I/O is time-bounded."""
    from redis import Redis

    return Redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_connect_timeout=BROKER_IO_TIMEOUT_SECONDS,
        socket_timeout=BROKER_IO_TIMEOUT_SECONDS,
        retry_on_timeout=False,
    )


def _published_at_ms(raw_message: Any) -> int | None:
    if raw_message is None:
        return None
    try:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode('utf-8')
        message = json.loads(raw_message)
        value = message.get('headers', {}).get(ENQUEUED_AT_HEADER)
        if isinstance(value, bool):
            return None
        value = int(value)
        return value if value >= 0 else None
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _collect_ready_queues(*, observed_at: float) -> dict[str, dict[str, Any]]:
    queue_names = declared_queue_names()
    result: dict[str, dict[str, Any]] = {
        queue: {
            'ready_depth': 0,
            'oldest_ready_age_seconds': None,
            'age_status': 'empty',
            'subscribed_workers': None,
            'active_count': None,
            'max_active_age_seconds': None,
        }
        for queue in queue_names
    }

    queue_keys = {queue: _broker_queue_keys(queue) for queue in queue_names}
    client = _redis_client()
    try:
        with client.pipeline(transaction=False) as pipeline:
            for queue in queue_names:
                for key in queue_keys[queue]:
                    pipeline.llen(key)
                    pipeline.lindex(key, -1)
            values = iter(pipeline.execute())

        for queue in queue_names:
            tails: list[Any] = []
            depth = 0
            for _key in queue_keys[queue]:
                size = next(values)
                tail = next(values)
                depth += int(size or 0)
                if size:
                    tails.append(tail)

            result[queue]['ready_depth'] = depth
            if depth == 0:
                continue

            published_values = [_published_at_ms(tail) for tail in tails]
            # Every non-empty priority bucket must have a trustworthy tail.
            # Otherwise an unknown bucket could contain the actual oldest item.
            if not published_values or any(
                value is None or value > observed_at * 1000
                for value in published_values
            ):
                result[queue]['age_status'] = 'unknown'
                continue
            oldest_ms = min(
                value for value in published_values if value is not None
            )
            result[queue]['age_status'] = 'known'
            result[queue]['oldest_ready_age_seconds'] = round(
                observed_at - (oldest_ms / 1000),
                3,
            )
    finally:
        client.close()
    return result


def _collect_worker_state(
    queues: dict[str, dict[str, Any]],
    *,
    observed_at: float,
) -> bool:
    connection = current_app.connection_for_read(
        connect_timeout=BROKER_IO_TIMEOUT_SECONDS,
        transport_options={
            **getattr(settings, 'CELERY_BROKER_TRANSPORT_OPTIONS', {}),
            'socket_connect_timeout': BROKER_IO_TIMEOUT_SECONDS,
            'socket_timeout': BROKER_IO_TIMEOUT_SECONDS,
            'retry_on_timeout': False,
        },
    )
    try:
        inspector = Inspect(
            app=current_app,
            connection=connection,
            timeout=BROKER_IO_TIMEOUT_SECONDS,
        )
        active_queues = inspector.active_queues()
        active_tasks = inspector.active(safe=True)
    finally:
        connection.close()
    if not isinstance(active_queues, dict) or not isinstance(active_tasks, dict):
        return False
    if not active_queues or set(active_queues) != set(active_tasks):
        return False

    subscribed = {queue: 0 for queue in queues}
    active_count = {queue: 0 for queue in queues}
    max_active_age: dict[str, float | None] = {
        queue: None for queue in queues
    }

    for worker_queues in active_queues.values():
        seen: set[str] = set()
        if not isinstance(worker_queues, list):
            return False
        for queue_info in worker_queues:
            queue = str((queue_info or {}).get('name') or '')
            if queue in subscribed and queue not in seen:
                subscribed[queue] += 1
                seen.add(queue)

    for worker_tasks in active_tasks.values():
        if not isinstance(worker_tasks, list):
            return False
        for task_info in worker_tasks:
            delivery_info = (task_info or {}).get('delivery_info') or {}
            queue = str(delivery_info.get('routing_key') or '')
            if queue not in active_count:
                continue
            active_count[queue] += 1
            started_at = (task_info or {}).get('time_start')
            if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
                age = max(0.0, observed_at - float(started_at))
                current = max_active_age[queue]
                max_active_age[queue] = age if current is None else max(current, age)

    for queue, data in queues.items():
        data['subscribed_workers'] = subscribed[queue]
        data['active_count'] = active_count[queue]
        active_age = max_active_age[queue]
        if active_age is not None:
            data['max_active_age_seconds'] = round(active_age, 3)
    return True


def collect_celery_queue_snapshot(*, now_timestamp: float | None = None) -> dict[str, Any]:
    """Collect one bounded snapshot; failures are explicit and credential-free."""
    observed_at = time.time() if now_timestamp is None else float(now_timestamp)
    snapshot: dict[str, Any] = {
        'schema_version': SNAPSHOT_SCHEMA_VERSION,
        'observed_at': observed_at,
        'collector_status': 'unavailable',
        'broker_status': 'unavailable',
        'worker_status': 'unavailable',
        'cache_status': 'not_attempted',
        'queues': {},
    }
    try:
        snapshot['queues'] = _collect_ready_queues(observed_at=observed_at)
        snapshot['broker_status'] = 'ok'
    except Exception:
        return snapshot

    try:
        workers_available = _collect_worker_state(
            snapshot['queues'],
            observed_at=observed_at,
        )
    except Exception:
        workers_available = False
    snapshot['worker_status'] = 'ok' if workers_available else 'unavailable'
    has_unknown_age = any(
        queue.get('age_status') == 'unknown'
        for queue in snapshot['queues'].values()
    )
    snapshot['collector_status'] = (
        'ok' if workers_available and not has_unknown_age else 'degraded'
    )
    return snapshot


def cache_celery_queue_snapshot(snapshot: dict[str, Any]) -> None:
    caches['coordination'].set(
        SNAPSHOT_CACHE_KEY,
        snapshot,
        timeout=SNAPSHOT_TTL_SECONDS,
    )


def get_cached_celery_queue_snapshot() -> dict[str, Any] | None:
    try:
        snapshot = caches['coordination'].get(SNAPSHOT_CACHE_KEY)
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get('schema_version') != SNAPSHOT_SCHEMA_VERSION:
        return None
    observed_at = snapshot.get('observed_at')
    if not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool):
        return None
    age = time.time() - float(observed_at)
    if age < -CACHE_FUTURE_SKEW_SECONDS or age > SNAPSHOT_TTL_SECONDS:
        return None
    return snapshot


def emit_celery_queue_snapshot_metrics(snapshot: dict[str, Any]) -> None:
    metric_gauge(
        'map.celery.collector.heartbeat',
        1,
    )
    metric_gauge(
        'map.celery.collector.broker_up',
        1 if snapshot.get('broker_status') == 'ok' else 0,
    )
    metric_gauge(
        'map.celery.collector.worker_inspect_up',
        1 if snapshot.get('worker_status') == 'ok' else 0,
    )
    metric_gauge(
        'map.celery.collector.cache_up',
        1 if snapshot.get('cache_status') == 'ok' else 0,
    )
    for queue, data in (snapshot.get('queues') or {}).items():
        attrs = {'queue': queue}
        metric_gauge('map.celery.queue.depth', data.get('ready_depth', 0), attributes=attrs)
        age_status = data.get('age_status')
        metric_gauge(
            'map.celery.queue.age_known',
            0 if age_status == 'unknown' else 1,
            attributes=attrs,
        )
        if age_status == 'empty':
            metric_gauge(
                'map.celery.queue.oldest_ready_age',
                0,
                unit='second',
                attributes=attrs,
            )
        elif data.get('oldest_ready_age_seconds') is not None:
            metric_gauge(
                'map.celery.queue.oldest_ready_age',
                data['oldest_ready_age_seconds'],
                unit='second',
                attributes=attrs,
            )
        if data.get('subscribed_workers') is not None:
            metric_gauge(
                'map.celery.queue.subscribed_workers',
                data['subscribed_workers'],
                attributes=attrs,
            )
        if data.get('active_count') is not None:
            metric_gauge(
                'map.celery.queue.active_count',
                data['active_count'],
                attributes=attrs,
            )
        if data.get('active_count') == 0:
            metric_gauge(
                'map.celery.queue.max_active_age',
                0,
                unit='second',
                attributes=attrs,
            )
        elif data.get('max_active_age_seconds') is not None:
            metric_gauge(
                'map.celery.queue.max_active_age',
                data['max_active_age_seconds'],
                unit='second',
                attributes=attrs,
            )
