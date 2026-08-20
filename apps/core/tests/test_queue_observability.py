import json
from unittest.mock import MagicMock, patch

from apps.core.celery_observability import ENQUEUED_AT_HEADER
from apps.core.queue_observability import (
    _broker_queue_keys,
    _collect_ready_queues,
    _collect_worker_state,
    collect_celery_queue_snapshot,
    emit_celery_queue_snapshot_metrics,
    get_cached_celery_queue_snapshot,
)
from apps.core.tasks import collect_celery_observability


class _Pipeline:
    def __init__(self, values):
        self.values = values
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def llen(self, key):
        self.commands.append(('llen', key))
        return self

    def lindex(self, key, index):
        self.commands.append(('lindex', key, index))
        return self

    def execute(self):
        return self.values


class _RedisClient:
    def __init__(self, values):
        self.pipeline_instance = _Pipeline(values)
        self.closed = False

    def pipeline(self, *, transaction):
        assert transaction is False
        return self.pipeline_instance

    def close(self):
        self.closed = True


def _message(published_at_ms):
    return json.dumps({'headers': {ENQUEUED_AT_HEADER: published_at_ms}})


def test_ready_snapshot_sums_priority_buckets_and_uses_oldest_known_tail(settings):
    settings.CELERY_TASK_QUEUES = {'billing': {}}
    settings.CELERY_BROKER_TRANSPORT_OPTIONS = {
        'global_keyprefix': 'map_broker_',
        'priority_steps': [0, 3],
        'sep': ':p',
    }
    client = _RedisClient([2, _message(9000), 3, _message(7000)])

    with patch(
        'apps.core.queue_observability._redis_client',
        return_value=client,
    ):
        queues = _collect_ready_queues(observed_at=10.0)

    assert queues['billing']['ready_depth'] == 5
    assert queues['billing']['age_status'] == 'known'
    assert queues['billing']['oldest_ready_age_seconds'] == 3.0
    assert client.pipeline_instance.commands == [
        ('llen', 'map_broker_billing'),
        ('lindex', 'map_broker_billing', -1),
        ('llen', 'map_broker_billing:p3'),
        ('lindex', 'map_broker_billing:p3', -1),
    ]
    assert client.closed is True


def test_physical_queue_keys_match_default_kombu_priority_contract(settings):
    settings.CELERY_BROKER_TRANSPORT_OPTIONS = {
        'global_keyprefix': 'map_broker_',
    }

    assert _broker_queue_keys('billing') == (
        'map_broker_billing',
        'map_broker_billing\x06\x163',
        'map_broker_billing\x06\x166',
        'map_broker_billing\x06\x169',
    )


def test_legacy_or_future_tail_is_unknown_not_zero(settings):
    settings.CELERY_TASK_QUEUES = {'billing': {}}
    settings.CELERY_BROKER_TRANSPORT_OPTIONS = {'priority_steps': [0, 3]}
    for tail in ('legacy', _message(11_000)):
        client = _RedisClient([1, tail, 0, None])
        with patch(
            'apps.core.queue_observability._redis_client',
            return_value=client,
        ):
            queues = _collect_ready_queues(observed_at=10.0)

        assert queues['billing']['age_status'] == 'unknown'
        assert queues['billing']['oldest_ready_age_seconds'] is None


def test_mixed_stamped_and_legacy_priority_tails_are_unknown(settings):
    settings.CELERY_TASK_QUEUES = {'billing': {}}
    settings.CELERY_BROKER_TRANSPORT_OPTIONS = {'priority_steps': [0, 3]}
    client = _RedisClient([1, 'legacy', 1, _message(7000)])

    with patch('apps.core.queue_observability._redis_client', return_value=client):
        queues = _collect_ready_queues(observed_at=10.0)

    assert queues['billing']['ready_depth'] == 2
    assert queues['billing']['age_status'] == 'unknown'
    assert queues['billing']['oldest_ready_age_seconds'] is None


def test_partial_worker_inspect_is_unavailable(settings):
    settings.CELERY_TASK_QUEUES = {'billing': {}}
    queues = {'billing': {
        'ready_depth': 0,
        'oldest_ready_age_seconds': None,
        'age_status': 'empty',
        'subscribed_workers': None,
        'active_count': None,
        'max_active_age_seconds': None,
    }}
    inspector = MagicMock()
    inspector.active_queues.return_value = {'worker-main': [{'name': 'billing'}]}
    inspector.active.return_value = {}

    connection = MagicMock()
    with (
        patch(
            'apps.core.queue_observability.current_app.connection_for_read',
            return_value=connection,
        ),
        patch('apps.core.queue_observability.Inspect', return_value=inspector),
    ):
        assert _collect_worker_state(queues, observed_at=10.0) is False
    inspector.active.assert_called_once_with(safe=True)
    connection.close.assert_called_once_with()
    assert queues['billing']['subscribed_workers'] is None


def test_idle_worker_state_reports_zero_active_tasks(settings):
    settings.CELERY_TASK_QUEUES = {'billing': {}}
    queues = {'billing': {
        'ready_depth': 0,
        'oldest_ready_age_seconds': None,
        'age_status': 'empty',
        'subscribed_workers': None,
        'active_count': None,
        'max_active_age_seconds': None,
    }}
    inspector = MagicMock()
    inspector.active_queues.return_value = {
        'worker-main': [{'name': 'billing'}],
    }
    inspector.active.return_value = {'worker-main': []}
    connection = MagicMock()

    with (
        patch(
            'apps.core.queue_observability.current_app.connection_for_read',
            return_value=connection,
        ),
        patch('apps.core.queue_observability.Inspect', return_value=inspector),
    ):
        assert _collect_worker_state(queues, observed_at=10.0) is True

    assert queues['billing']['subscribed_workers'] == 1
    assert queues['billing']['active_count'] == 0


def test_broker_failure_is_explicit_and_does_not_expose_exception(settings):
    settings.CELERY_TASK_QUEUES = {'billing': {}}
    with patch(
        'apps.core.queue_observability._redis_client',
        side_effect=RuntimeError('redis://user:secret@broker/0'),
    ):
        snapshot = collect_celery_queue_snapshot(now_timestamp=10.0)

    assert snapshot == {
        'schema_version': 1,
        'observed_at': 10.0,
        'collector_status': 'unavailable',
        'broker_status': 'unavailable',
        'worker_status': 'unavailable',
        'cache_status': 'not_attempted',
        'queues': {},
    }
    assert 'secret' not in json.dumps(snapshot)


def test_cached_snapshot_rejects_stale_future_and_wrong_schema(monkeypatch):
    cache = MagicMock()
    snapshot = {
        'schema_version': 1,
        'observed_at': 100.0,
        'queues': {},
    }
    monkeypatch.setattr('apps.core.queue_observability.caches', {'coordination': cache})
    monkeypatch.setattr('apps.core.queue_observability.time.time', lambda: 100.0)

    cache.get.return_value = snapshot
    assert get_cached_celery_queue_snapshot() == snapshot

    cache.get.return_value = {**snapshot, 'observed_at': -100.0}
    assert get_cached_celery_queue_snapshot() is None

    cache.get.return_value = {**snapshot, 'observed_at': 106.0}
    assert get_cached_celery_queue_snapshot() is None

    cache.get.return_value = {**snapshot, 'schema_version': 999}
    assert get_cached_celery_queue_snapshot() is None


def test_snapshot_metrics_reset_empty_queue_and_expose_unknown_age():
    snapshot = {
        'broker_status': 'ok',
        'worker_status': 'ok',
        'cache_status': 'ok',
        'queues': {
            'billing': {
                'ready_depth': 0,
                'age_status': 'empty',
                'oldest_ready_age_seconds': None,
                'subscribed_workers': 1,
                'active_count': 0,
                'max_active_age_seconds': None,
            },
            'notifications': {
                'ready_depth': 1,
                'age_status': 'unknown',
                'oldest_ready_age_seconds': None,
                'subscribed_workers': 1,
                'active_count': 0,
                'max_active_age_seconds': None,
            },
        },
    }

    with patch('apps.core.queue_observability.metric_gauge') as gauge:
        emit_celery_queue_snapshot_metrics(snapshot)

    assert any(
        call.args[:2] == ('map.celery.queue.oldest_ready_age', 0)
        and call.kwargs.get('attributes') == {'queue': 'billing'}
        for call in gauge.call_args_list
    )
    assert any(
        call.args[:2] == ('map.celery.queue.age_known', 0)
        and call.kwargs.get('attributes') == {'queue': 'notifications'}
        for call in gauge.call_args_list
    )


def test_collector_cache_failure_is_degraded_but_still_emits_metrics():
    snapshot = {
        'collector_status': 'ok',
        'broker_status': 'ok',
        'worker_status': 'ok',
        'cache_status': 'not_attempted',
        'queues': {},
    }
    with (
        patch(
            'apps.core.queue_observability.collect_celery_queue_snapshot',
            return_value=snapshot,
        ),
        patch(
            'apps.core.queue_observability.cache_celery_queue_snapshot',
            side_effect=RuntimeError('cache unavailable'),
        ),
        patch(
            'apps.core.queue_observability.emit_celery_queue_snapshot_metrics',
        ) as emit,
    ):
        result = collect_celery_observability()

    assert result['collector_status'] == 'degraded'
    assert result['cache_status'] == 'unavailable'
    emit.assert_called_once_with(result)
