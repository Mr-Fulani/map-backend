from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.core.dispatch import (
    DURABLE_TASK_QUEUES,
    SafeRetryableDispatchError,
    enqueue_durable_task,
)
from apps.core.models import BackgroundJobDispatch
from apps.marketplaces.models import MarketplaceAccount
from apps.marketplaces.tasks import (
    dispatch_due_marketplace_feed_intents,
    process_marketplace_feed_intent,
)
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db

_INTENT_TASK = 'apps.marketplaces.tasks.process_marketplace_feed_intent'


@pytest.fixture(autouse=True)
def _durable_ingress_mode(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'durable'


def _account(
    suffix: str,
    *,
    tenant_active: bool = True,
    is_active: bool = True,
    deleted_at=None,
    revision: int = 0,
    dispatched_revision: int = 0,
    due_at=None,
) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Feed dispatch {suffix}',
        slug=f'feed-dispatch-{suffix}'[:50],
        is_active=tenant_active,
    )
    return MarketplaceAccount.all_objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Feed dispatch account {suffix}',
        external_id=f'feed-dispatch-{suffix}',
        credentials_enc=b'opaque-test-credentials',
        is_active=is_active,
        deleted_at=deleted_at,
        feed_intent_revision=revision,
        feed_intent_dispatched_revision=dispatched_revision,
        feed_intent_due_at=due_at,
    )


def test_feed_intent_worker_is_allowlisted_on_avito_publish():
    assert DURABLE_TASK_QUEUES[_INTENT_TASK] == 'avito_publish'


def test_periodic_setup_registers_minute_feed_intent_scanner():
    call_command('setup_periodic_tasks', stdout=StringIO())

    periodic = PeriodicTask.objects.select_related('interval').get(
        name='dispatch_due_marketplace_feed_intents',
    )
    assert periodic.task == (
        'apps.marketplaces.tasks.dispatch_due_marketplace_feed_intents'
    )
    assert periodic.queue == 'avito_publish'
    assert periodic.expire_seconds == 50
    assert periodic.interval.every == 1
    assert periodic.interval.period == 'minutes'


def test_dual_write_scanner_runs_legacy_repair_without_private_dispatch(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'

    result = dispatch_due_marketplace_feed_intents()

    assert result == {
        'status': 'legacy_repair',
        'selected': 0,
        'enqueued': 0,
        'owned': 0,
        'failed': 0,
        'batch_limit': 100,
        'revisions': [],
    }
    assert BackgroundJobDispatch.objects.count() == 0


def test_scanner_persists_exact_dispatch_and_advances_cursor_without_updated_at():
    due_at = timezone.now() - timedelta(minutes=1)
    account = _account(
        'exact',
        revision=7,
        dispatched_revision=6,
        due_at=due_at,
    )
    original_updated_at = account.updated_at

    result = dispatch_due_marketplace_feed_intents()

    dispatch = BackgroundJobDispatch.objects.get()
    account.refresh_from_db()
    assert result['status'] == 'dispatched'
    assert result['selected'] == 1
    assert result['enqueued'] == 1
    assert result['dispatch_ids'] == [str(dispatch.pk)]
    assert dispatch.task_name == _INTENT_TASK
    assert dispatch.queue == 'avito_publish'
    assert dispatch.args == [account.pk, 7]
    assert dispatch.kwargs == {}
    assert dispatch.deduplication_key == f'feed-intent:{account.pk}:rev:7'
    assert account.feed_intent_revision == 7
    assert account.feed_intent_dispatched_revision == 7
    assert account.feed_intent_due_at is None
    assert account.updated_at == original_updated_at


def test_scanner_filters_live_due_undispatched_accounts_only():
    current_time = timezone.now()
    eligible = _account(
        'eligible',
        revision=2,
        dispatched_revision=1,
        due_at=current_time - timedelta(seconds=1),
    )
    _account(
        'future',
        revision=2,
        dispatched_revision=1,
        due_at=current_time + timedelta(hours=1),
    )
    _account(
        'already-dispatched',
        revision=2,
        dispatched_revision=2,
        due_at=current_time - timedelta(seconds=1),
    )
    _account(
        'held',
        revision=2,
        dispatched_revision=1,
        due_at=None,
    )
    _account(
        'inactive-account',
        is_active=False,
        revision=2,
        dispatched_revision=1,
        due_at=current_time - timedelta(seconds=1),
    )
    _account(
        'inactive-tenant',
        tenant_active=False,
        revision=2,
        dispatched_revision=1,
        due_at=current_time - timedelta(seconds=1),
    )
    _account(
        'deleted',
        is_active=False,
        deleted_at=current_time,
        revision=2,
        dispatched_revision=1,
        due_at=current_time - timedelta(seconds=1),
    )

    result = dispatch_due_marketplace_feed_intents()

    assert result['selected'] == 1
    dispatch = BackgroundJobDispatch.objects.get()
    assert dispatch.args == [eligible.pk, 2]


def test_scanner_orders_by_due_then_account_id_and_honours_requested_limit():
    current_time = timezone.now()
    first_tie = _account(
        'order-first-tie',
        revision=1,
        due_at=current_time - timedelta(minutes=1),
    )
    _account(
        'order-second-tie',
        revision=1,
        due_at=current_time - timedelta(minutes=1),
    )
    oldest = _account(
        'order-oldest',
        revision=1,
        due_at=current_time - timedelta(minutes=2),
    )

    result = dispatch_due_marketplace_feed_intents(limit=2)

    dispatches = [
        BackgroundJobDispatch.objects.get(pk=dispatch_id)
        for dispatch_id in result['dispatch_ids']
    ]
    assert result['selected'] == 2
    assert [dispatch.args for dispatch in dispatches] == [
        [oldest.pk, 1],
        [first_tie.pk, 1],
    ]


def test_scanner_never_selects_more_than_one_hundred_accounts():
    due_at = timezone.now() - timedelta(seconds=1)
    tenants = Tenant.objects.bulk_create([
        Tenant(name=f'Batch tenant {index}', slug=f'feed-batch-{index}')
        for index in range(101)
    ])
    MarketplaceAccount.all_objects.bulk_create([
        MarketplaceAccount(
            tenant=tenant,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            name=f'Batch account {index}',
            external_id=f'feed-batch-{index}',
            credentials_enc=b'opaque-test-credentials',
            feed_intent_revision=1,
            feed_intent_dispatched_revision=0,
            feed_intent_due_at=due_at,
        )
        for index, tenant in enumerate(tenants)
    ])

    result = dispatch_due_marketplace_feed_intents(limit=10_000)

    assert result['batch_limit'] == 100
    assert result['selected'] == 100
    assert BackgroundJobDispatch.objects.count() == 100
    assert MarketplaceAccount.objects.filter(
        feed_intent_revision__gt=0,
        feed_intent_due_at__isnull=False,
    ).count() == 1


def test_existing_dedup_dispatch_repairs_cursor_without_duplicate_row():
    due_at = timezone.now() - timedelta(seconds=1)
    account = _account(
        'existing-dedup',
        revision=3,
        dispatched_revision=2,
        due_at=due_at,
    )
    existing = BackgroundJobDispatch.objects.create(
        task_name=_INTENT_TASK,
        queue='avito_publish',
        args=[account.pk, 3],
        deduplication_key=f'feed-intent:{account.pk}:rev:3',
    )

    result = dispatch_due_marketplace_feed_intents()

    account.refresh_from_db()
    assert result['dispatch_ids'] == [str(existing.pk)]
    assert BackgroundJobDispatch.objects.count() == 1
    assert account.feed_intent_dispatched_revision == 3
    assert account.feed_intent_due_at is None


def test_conflicting_dedup_dispatch_rolls_back_without_advancing_cursor():
    due_at = timezone.now() - timedelta(seconds=1)
    account = _account(
        'conflicting-dedup',
        revision=3,
        dispatched_revision=2,
        due_at=due_at,
    )
    BackgroundJobDispatch.objects.create(
        task_name='apps.marketplaces.tasks.process_marketplace_feed_run_step',
        queue='avito_publish',
        args=['00000000-0000-0000-0000-000000000000', 3],
        deduplication_key=f'feed-intent:{account.pk}:rev:3',
    )

    with pytest.raises(RuntimeError, match='Conflicting durable dispatch'):
        dispatch_due_marketplace_feed_intents()

    account.refresh_from_db()
    assert account.feed_intent_dispatched_revision == 2
    assert account.feed_intent_due_at == due_at


def test_scanner_rolls_back_dispatch_and_all_cursors_on_mid_batch_failure():
    due_at = timezone.now() - timedelta(seconds=1)
    first = _account('rollback-first', revision=1, due_at=due_at)
    second = _account('rollback-second', revision=1, due_at=due_at)
    calls = 0

    def enqueue_then_fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('synthetic outbox failure')
        return enqueue_durable_task(*args, **kwargs)

    with patch(
        'apps.marketplaces.tasks.enqueue_durable_task',
        side_effect=enqueue_then_fail,
    ):
        with pytest.raises(RuntimeError, match='synthetic outbox failure'):
            dispatch_due_marketplace_feed_intents()

    first.refresh_from_db()
    second.refresh_from_db()
    assert BackgroundJobDispatch.objects.count() == 0
    assert first.feed_intent_dispatched_revision == 0
    assert second.feed_intent_dispatched_revision == 0
    assert first.feed_intent_due_at == due_at
    assert second.feed_intent_due_at == due_at


def test_dark_worker_fences_stale_future_and_exact_without_provider_io():
    account = _account(
        'worker-revisions',
        revision=5,
        dispatched_revision=5,
    )

    provider_call = AssertionError('dark intent worker crossed provider boundary')
    with (
        patch('apps.marketplaces.tasks.AvitoAdapter', side_effect=provider_call),
        patch('apps.marketplaces.tasks.build_feed', side_effect=provider_call),
        patch('apps.marketplaces.tasks.build_stop_feed', side_effect=provider_call),
        patch(
            'apps.marketplaces.tasks.create_or_supersede_feed_run',
            side_effect=provider_call,
        ),
    ):
        stale = process_marketplace_feed_intent(account.pk, 4)
        exact = process_marketplace_feed_intent(account.pk, 5)
        future = process_marketplace_feed_intent(account.pk, 6)

    assert stale == {'status': 'stale'}
    assert exact == {
        'status': 'not_activated',
        'account_id': account.pk,
        'revision': 5,
    }
    assert future == {'status': 'future_revision'}
    assert MarketplaceAccount.objects.filter(pk=account.pk).exists()
    assert BackgroundJobDispatch.objects.count() == 0


def test_dark_worker_rejects_current_revision_not_dispatched_by_scanner():
    account = _account(
        'worker-undispatched',
        revision=5,
        dispatched_revision=4,
    )

    assert process_marketplace_feed_intent(account.pk, 5) == {
        'status': 'future_revision',
    }


def test_dark_worker_handles_disabled_invalid_missing_and_inactive_without_io(
    settings,
    django_assert_num_queries,
):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'
    with django_assert_num_queries(0):
        with pytest.raises(SafeRetryableDispatchError, match='ingress is disabled'):
            process_marketplace_feed_intent(1, 1)

    settings.MARKETPLACE_FEED_INGRESS_MODE = 'durable'
    with django_assert_num_queries(0):
        assert process_marketplace_feed_intent(True, 1) == {'status': 'invalid'}
        assert process_marketplace_feed_intent(1, 0) == {'status': 'invalid'}
    with pytest.raises(SafeRetryableDispatchError, match='temporarily unavailable'):
        process_marketplace_feed_intent(999_999, 1)

    inactive = _account(
        'worker-inactive',
        is_active=False,
        revision=1,
        dispatched_revision=1,
    )
    with pytest.raises(SafeRetryableDispatchError, match='owner is inactive'):
        process_marketplace_feed_intent(inactive.pk, 1)
