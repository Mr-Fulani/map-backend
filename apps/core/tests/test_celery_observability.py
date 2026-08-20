from types import SimpleNamespace
from unittest.mock import patch

from apps.core import celery_observability
from apps.core.celery_observability import (
    ENQUEUED_AT_HEADER,
    FIRST_PUBLISHED_AT_HEADER,
    observe_task_postrun,
    observe_task_prerun,
    observe_task_retry,
    stamp_task_publish,
)
from apps.core.telemetry import safe_metric_attributes


def test_publish_stamp_refreshes_queue_time_and_preserves_first_publish(monkeypatch):
    headers = {FIRST_PUBLISHED_AT_HEADER: 100}
    monkeypatch.setattr(celery_observability.time, 'time', lambda: 2.5)

    stamp_task_publish(headers=headers)

    assert headers[ENQUEUED_AT_HEADER] == 2500
    assert headers[FIRST_PUBLISHED_AT_HEADER] == 100


def test_task_runtime_timer_is_cleaned_and_uses_bounded_dimensions():
    celery_observability._active_timers.clear()
    task = SimpleNamespace(
        name='apps.marketplaces.tasks.check_moderation_task',
        request=SimpleNamespace(delivery_info={'routing_key': 'avito_update'}),
    )
    monotonic_values = iter([10.0, 12.5])
    with patch(
        'apps.core.celery_observability.time.monotonic',
        side_effect=lambda: next(monotonic_values),
    ):
        with patch('apps.core.celery_observability.metric_distribution') as distribution:
            observe_task_prerun(task_id='sensitive-task-id')
            observe_task_postrun(task_id='sensitive-task-id', task=task, state='SUCCESS')

    assert 'sensitive-task-id' not in celery_observability._active_timers
    distribution.assert_called_once_with(
        'map.celery.task.runtime',
        2.5,
        unit='second',
        attributes={
            'task_family': 'marketplace',
            'queue': 'avito_update',
            'outcome': 'success',
        },
    )


def test_retry_metric_does_not_include_task_or_entity_identifiers():
    request = SimpleNamespace(
        id='task-123',
        args=[987],
        delivery_info={'routing_key': 'billing'},
    )
    task = SimpleNamespace(name='apps.billing.tasks.reconcile_yookassa_billing')

    with patch('apps.core.celery_observability.metric_count') as count:
        observe_task_retry(sender=task, request=request, reason=RuntimeError('secret'))

    count.assert_called_once_with(
        'map.celery.task.execution',
        attributes={
            'task_family': 'billing',
            'queue': 'billing',
            'outcome': 'retry',
        },
    )


def test_metric_attribute_allowlist_collapses_unbounded_values():
    attributes = safe_metric_attributes({
        'queue': 'tenant-123-private-queue',
        'task_family': 'apps.secret.task.123',
        'outcome': 'failure-for-account-456',
        'tenant_id': '456',
        'url': 'https://credentials.example/path',
    })

    assert attributes == {
        'queue': 'other',
        'task_family': 'other',
        'outcome': 'other',
    }


def test_metric_backend_failure_is_fail_open():
    from apps.core.telemetry import metric_count

    with patch(
        'apps.core.telemetry.sentry_metrics.count',
        side_effect=RuntimeError('telemetry unavailable'),
    ):
        metric_count(
            'map.celery.task.execution',
            attributes={
                'task_family': 'billing',
                'queue': 'billing',
                'outcome': 'success',
            },
        )
