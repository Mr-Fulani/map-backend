from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.dispatch import recover_terminal_feed_intent_dispatches
from apps.core.models import BackgroundJobDispatch
from apps.marketplaces.tasks import dispatch_due_marketplace_feed_intents
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.tenants.models import Tenant


TASK_NAME = 'apps.marketplaces.tasks.process_marketplace_feed_intent'


def _account(
    suffix: str,
    *,
    revision: int,
    dispatched_revision: int,
    is_active: bool = True,
) -> tuple[MarketplaceAccount, MarketplaceFeedEndpoint]:
    tenant = Tenant.objects.create(
        name=f'Feed recovery {suffix}',
        slug=f'feed-recovery-{suffix}',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Feed recovery account {suffix}',
        external_id=f'feed-recovery-{suffix}',
        credentials_enc=b'opaque-test-credentials',
        is_active=is_active,
        feed_intent_revision=revision,
        feed_intent_dispatched_revision=dispatched_revision,
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest='a' * 64,
        source_intent_revision=revision,
    )
    return account, endpoint


def _terminal_dispatch(
    account: MarketplaceAccount,
    revision: int,
    *,
    status=BackgroundJobDispatch.Status.FAILED,
    result=None,
) -> BackgroundJobDispatch:
    return BackgroundJobDispatch.objects.create(
        task_name=TASK_NAME,
        queue='avito_publish',
        args=[account.pk, revision],
        status=status,
        run_attempts=5,
        max_run_attempts=5,
        result=result,
        finished_at=timezone.now(),
    )


@pytest.mark.django_db
def test_terminal_feed_intent_recovery_rearms_exact_dispatched_revision():
    account, endpoint = _account(
        'exact',
        revision=5,
        dispatched_revision=5,
    )
    dispatch = _terminal_dispatch(account, 5)
    before = timezone.now()

    result = recover_terminal_feed_intent_dispatches()

    account.refresh_from_db()
    endpoint.refresh_from_db()
    dispatch.refresh_from_db()
    assert result == {
        'selected': 1,
        'recovered': 1,
        'superseded': 0,
        'held': 0,
        'invalid': 0,
    }
    assert account.feed_intent_revision == 6
    assert account.feed_intent_dispatched_revision == 5
    assert account.feed_intent_due_at >= before
    assert endpoint.source_intent_revision == 6
    assert dispatch.result == {
        'reason_code': 'feed_intent_recovered',
        'source_revision': 5,
        'desired_revision': 6,
    }


@pytest.mark.django_db
def test_terminal_recovery_nudges_newer_held_desired_revision_without_bump():
    account, endpoint = _account(
        'newer-held',
        revision=7,
        dispatched_revision=5,
    )
    dispatch = _terminal_dispatch(account, 5)

    result = recover_terminal_feed_intent_dispatches()

    account.refresh_from_db()
    endpoint.refresh_from_db()
    dispatch.refresh_from_db()
    assert result['recovered'] == 1
    assert account.feed_intent_revision == 7
    assert account.feed_intent_dispatched_revision == 5
    assert account.feed_intent_due_at is not None
    assert endpoint.source_intent_revision == 7
    assert dispatch.result['desired_revision'] == 7


@pytest.mark.django_db
def test_terminal_recovery_marks_older_dispatch_superseded():
    account, endpoint = _account(
        'superseded',
        revision=8,
        dispatched_revision=8,
    )
    dispatch = _terminal_dispatch(account, 5)

    result = recover_terminal_feed_intent_dispatches()

    account.refresh_from_db()
    endpoint.refresh_from_db()
    dispatch.refresh_from_db()
    assert result['superseded'] == 1
    assert account.feed_intent_revision == 8
    assert account.feed_intent_dispatched_revision == 8
    assert account.feed_intent_due_at is None
    assert endpoint.source_intent_revision == 8
    assert dispatch.result['reason_code'] == 'feed_intent_superseded'


@pytest.mark.django_db
def test_terminal_recovery_holds_inactive_owner_for_reactivation():
    account, endpoint = _account(
        'inactive',
        revision=5,
        dispatched_revision=5,
        is_active=False,
    )
    dispatch = _terminal_dispatch(account, 5)
    before = timezone.now()

    result = recover_terminal_feed_intent_dispatches()

    account.refresh_from_db()
    endpoint.refresh_from_db()
    dispatch.refresh_from_db()
    assert result['held'] == 1
    assert account.feed_intent_revision == 5
    assert account.feed_intent_due_at is None
    assert endpoint.source_intent_revision == 5
    assert dispatch.result is None
    assert dispatch.available_at >= before + timedelta(minutes=4)


@pytest.mark.django_db
def test_old_disabled_success_is_recovered_once_and_then_ignored():
    account, endpoint = _account(
        'old-dark',
        revision=3,
        dispatched_revision=3,
    )
    dispatch = _terminal_dispatch(
        account,
        3,
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        result={'status': 'disabled', 'account_id': account.pk, 'revision': 3},
    )

    first = recover_terminal_feed_intent_dispatches()
    second = recover_terminal_feed_intent_dispatches()

    account.refresh_from_db()
    endpoint.refresh_from_db()
    dispatch.refresh_from_db()
    assert first['recovered'] == 1
    assert second['selected'] == 0
    assert account.feed_intent_revision == 4
    assert endpoint.source_intent_revision == 4
    assert dispatch.result['reason_code'] == 'feed_intent_recovered'


@pytest.mark.django_db
def test_current_not_activated_checkpoint_does_not_create_successor_loop(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'durable'
    account, endpoint = _account(
        'not-activated-hold',
        revision=3,
        dispatched_revision=3,
    )
    dispatch = _terminal_dispatch(
        account,
        3,
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        result={
            'status': 'not_activated',
            'account_id': account.pk,
            'revision': 3,
        },
    )
    before = timezone.now()

    first = recover_terminal_feed_intent_dispatches()
    second = recover_terminal_feed_intent_dispatches()
    scan = dispatch_due_marketplace_feed_intents()

    account.refresh_from_db()
    endpoint.refresh_from_db()
    dispatch.refresh_from_db()
    assert first == {
        'selected': 1,
        'recovered': 0,
        'superseded': 0,
        'held': 1,
        'invalid': 0,
    }
    assert second['selected'] == 0
    assert scan['selected'] == 0
    assert account.feed_intent_revision == 3
    assert account.feed_intent_dispatched_revision == 3
    assert account.feed_intent_due_at is None
    assert endpoint.source_intent_revision == 3
    assert BackgroundJobDispatch.objects.count() == 1
    assert dispatch.available_at >= before + timedelta(minutes=59)


@pytest.mark.django_db
def test_malformed_terminal_feed_intent_is_closed_without_domain_write():
    dispatch = BackgroundJobDispatch.objects.create(
        task_name=TASK_NAME,
        queue='avito_publish',
        args=[True, 'not-a-revision'],
        status=BackgroundJobDispatch.Status.FAILED,
        finished_at=timezone.now(),
    )

    result = recover_terminal_feed_intent_dispatches()

    dispatch.refresh_from_db()
    assert result['invalid'] == 1
    assert dispatch.result == {
        'reason_code': 'feed_intent_recovery_invalid',
        'source_revision': None,
        'desired_revision': None,
    }
