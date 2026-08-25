import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.core.models import BackgroundJobDispatch
from apps.datasources.encryption import encrypt
from apps.marketplaces.feed_workflow import (
    FeedAccountUnavailable,
    FeedRunConflict,
    claim_due_run_for_account,
    create_or_supersede_feed_run,
    finish_feed_run,
    mark_feed_submission_unknown,
    reconcile_uncertain_feed_run,
)
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.products.models import Product
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


def _account(suffix: str) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Reconcile {suffix}',
        slug=f'reconcile-{suffix}',
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Account {suffix}',
        external_id=f'account-{suffix}',
        credentials_enc=encrypt({
            'client_id': f'client-{suffix}',
            'client_secret': 'secret',
        }),
    )


def _listing(account: MarketplaceAccount, suffix: str) -> Listing:
    product = Product.objects.create(
        tenant_id=account.tenant_id,
        article=f'RECONCILE-{suffix}',
        name=f'Reconcile product {suffix}',
        price=Decimal('1000.00'),
    )
    return Listing.objects.create(
        tenant_id=account.tenant_id,
        account=account,
        product=product,
        status=Listing.STATUS_PENDING,
        price_on_listing=Decimal('1100.00'),
    )


def _uncertain_run(suffix: str):
    started_at = timezone.now() - timedelta(hours=3)
    account = _account(suffix)
    listing = _listing(account, suffix)
    run = create_or_supersede_feed_run(account.pk, now=started_at)
    preparing_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        now=started_at,
    )
    assert preparing_claim is not None
    unknown = mark_feed_submission_unknown(
        preparing_claim,
        submitted_at=started_at,
        next_attempt_at=started_at + timedelta(seconds=1),
        error='lost provider response',
        now=started_at,
    )
    unknown_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=unknown.revision,
        now=started_at + timedelta(seconds=1),
    )
    assert unknown_claim is not None
    uncertain = finish_feed_run(
        unknown_claim,
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        error='manual reconciliation required',
        increment_submission_attempt=True,
        now=started_at + timedelta(seconds=1),
    )
    return account, listing, uncertain


def _command(
    run,
    *,
    resolution,
    apply=False,
    provider_run_id=None,
):
    stdout = StringIO()
    call_command(
        'reconcile_marketplace_feed_run',
        str(run.pk),
        expected_revision=run.revision,
        resolution=resolution,
        provider_run_id=provider_run_id,
        apply=apply,
        stdout=stdout,
    )
    return json.loads(stdout.getvalue())


def test_dry_run_validates_exact_transition_without_mutating_or_dispatching():
    _account_row, listing, uncertain = _uncertain_run('dry-run')

    summary = _command(
        uncertain,
        resolution='provider_accepted',
        provider_run_id='upload-reviewed-1',
    )

    current = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    listing.refresh_from_db()
    assert summary['mode'] == 'dry_run'
    assert summary['state'] == MarketplaceFeedRun.State.POLLING
    assert summary['revision'] == uncertain.revision + 1
    assert summary['dispatch_id'] is None
    assert summary['flush_scheduled'] is False
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.revision == uncertain.revision
    assert current.provider_run_id is None
    assert listing.feed_run_id == uncertain.pk
    assert BackgroundJobDispatch.objects.count() == 0


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='dual_write',
    MARKETPLACE_FEED_RUN_MODE='durable',
)
def test_provider_accepted_binds_exact_run_and_persists_revision_dispatch(
    django_capture_on_commit_callbacks,
):
    _account_row, listing, uncertain = _uncertain_run('accepted')
    MarketplaceFeedRun.objects.filter(pk=uncertain.pk).update(report_attempt=3)

    with django_capture_on_commit_callbacks(execute=False):
        summary = _command(
            uncertain,
            resolution='provider_accepted',
            provider_run_id=' upload-reviewed-accepted ',
            apply=True,
        )

    current = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    dispatch = BackgroundJobDispatch.objects.get(pk=summary['dispatch_id'])
    listing.refresh_from_db()
    assert summary['mode'] == 'apply'
    assert current.state == MarketplaceFeedRun.State.POLLING
    assert current.revision == uncertain.revision + 1
    assert current.provider_run_id == 'upload-reviewed-accepted'
    assert current.next_attempt_at is not None
    assert current.finished_at is None
    assert current.submission_reconcile_attempt == 0
    assert current.report_attempt == 0
    assert 'Operator reconciled' in current.last_error
    assert listing.feed_run_id == uncertain.pk
    assert dispatch.task_name == 'apps.marketplaces.tasks.process_marketplace_feed_run_step'
    assert dispatch.args == [str(uncertain.pk), current.revision]
    assert dispatch.deduplication_key == (
        f'feed-run:{uncertain.pk}:rev:{current.revision}'
    )


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='dual_write',
    MARKETPLACE_FEED_RUN_MODE='durable',
)
def test_provider_not_accepted_closes_run_and_schedules_fresh_flush_after_commit(
    django_capture_on_commit_callbacks,
):
    account, listing, uncertain = _uncertain_run('not-accepted')

    with patch('apps.marketplaces.tasks.coalesced_flush_task.delay') as flush:
        with django_capture_on_commit_callbacks(execute=True):
            summary = _command(
                uncertain,
                resolution='provider_not_accepted',
                apply=True,
            )

    current = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    dispatch = BackgroundJobDispatch.objects.get(pk=summary['dispatch_id'])
    listing.refresh_from_db()
    assert current.state == MarketplaceFeedRun.State.FAILED
    assert current.revision == uncertain.revision + 1
    assert current.next_attempt_at is None
    assert current.finished_at is not None
    assert listing.feed_run_id == uncertain.pk
    assert summary['flush_scheduled'] is True
    assert dispatch.task_name == 'apps.marketplaces.tasks.process_marketplace_feed_run_step'
    assert dispatch.args == [str(uncertain.pk), current.revision]
    assert dispatch.deduplication_key == (
        f'feed-run:{uncertain.pk}:rev:{current.revision}'
    )
    flush.assert_called_once_with(account.pk)

    fresh = create_or_supersede_feed_run(
        account.pk,
        now=timezone.now() + timedelta(seconds=1),
    )
    listing.refresh_from_db()
    assert fresh.state == MarketplaceFeedRun.State.PREPARING
    assert listing.feed_run_id == fresh.pk


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='dual_write',
    MARKETPLACE_FEED_RUN_MODE='durable',
)
def test_provider_not_accepted_rolls_back_when_terminal_dispatch_is_missing(
    django_capture_on_commit_callbacks,
):
    _account_row, listing, uncertain = _uncertain_run('missing-terminal-dispatch')

    with (
        patch(
            'apps.marketplaces.tasks._enqueue_feed_run_snapshot',
            return_value=None,
        ),
        patch('apps.marketplaces.tasks.coalesced_flush_task.delay') as flush,
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(CommandError) as error,
    ):
        _command(
            uncertain,
            resolution='provider_not_accepted',
            apply=True,
        )

    detail = json.loads(str(error.value))
    current = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    listing.refresh_from_db()
    assert detail['error_code'] == 'reconciliation_refused'
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.revision == uncertain.revision
    assert current.finished_at == uncertain.finished_at
    assert listing.feed_run_id == uncertain.pk
    assert callbacks == []
    assert BackgroundJobDispatch.objects.count() == 0
    flush.assert_not_called()


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='legacy',
    MARKETPLACE_FEED_RUN_MODE='legacy',
)
def test_not_accepted_in_legacy_mode_does_not_schedule_automatic_flush():
    _account_row, _listing_row, uncertain = _uncertain_run('legacy-close')

    with patch('apps.marketplaces.tasks.coalesced_flush_task.delay') as flush:
        summary = _command(
            uncertain,
            resolution='provider_not_accepted',
            apply=True,
        )

    assert summary['state'] == MarketplaceFeedRun.State.FAILED
    assert summary['flush_scheduled'] is False
    assert summary['dispatch_id'] is None
    assert BackgroundJobDispatch.objects.count() == 0
    flush.assert_not_called()


def test_reconciliation_refuses_stale_non_uncertain_inactive_and_changed_identity():
    account, _listing_row, uncertain = _uncertain_run('fences')

    with pytest.raises(FeedRunConflict, match='revision changed'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision + 1,
            resolution='provider_not_accepted',
        )

    MarketplaceFeedRun.objects.filter(pk=uncertain.pk).update(
        state=MarketplaceFeedRun.State.FAILED,
    )
    with pytest.raises(FeedRunConflict, match='not awaiting'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_not_accepted',
        )

    MarketplaceFeedRun.objects.filter(pk=uncertain.pk).update(
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    )
    account.is_active = False
    account.save(update_fields=['is_active'])
    with pytest.raises(FeedAccountUnavailable, match='inactive'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_not_accepted',
        )

    account.is_active = True
    account.credentials_enc = encrypt({
        'client_id': 'rotated-client',
        'client_secret': 'rotated-secret',
    })
    account.save(update_fields=['is_active', 'credentials_enc'])
    with pytest.raises(FeedRunConflict, match='identity changed'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_not_accepted',
        )


def test_reconciliation_refuses_missing_submission_and_invalid_provider_binding():
    _account_row, _listing_row, uncertain = _uncertain_run('invalid')
    MarketplaceFeedRun.objects.filter(pk=uncertain.pk).update(submitted_at=None)
    with pytest.raises(FeedRunConflict, match='without a submission timestamp'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_accepted',
            provider_run_id='upload-invalid',
        )

    MarketplaceFeedRun.objects.filter(pk=uncertain.pk).update(
        submitted_at=timezone.now() - timedelta(hours=3),
    )
    with pytest.raises(ValueError, match='provider_run_id'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_accepted',
            provider_run_id=' ',
        )
    with pytest.raises(ValueError, match='allowed only'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_not_accepted',
            provider_run_id='upload-must-not-be-used',
        )
    with pytest.raises(ValueError, match='resolution'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='retry_post',
        )


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='legacy',
    MARKETPLACE_FEED_RUN_MODE='legacy',
)
def test_accepted_apply_is_refused_when_durable_runtime_is_disabled():
    _account_row, _listing_row, uncertain = _uncertain_run('disabled')

    with pytest.raises(CommandError) as error:
        _command(
            uncertain,
            resolution='provider_accepted',
            provider_run_id='upload-disabled',
            apply=True,
        )

    detail = json.loads(str(error.value))
    current = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    assert detail['error_code'] == 'runtime_mode_disabled'
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.revision == uncertain.revision


def test_command_error_is_structured_and_does_not_expose_credentials():
    account, _listing_row, uncertain = _uncertain_run('structured-error')

    with pytest.raises(CommandError) as error:
        call_command(
            'reconcile_marketplace_feed_run',
            str(uncertain.pk),
            expected_revision=uncertain.revision + 1,
            resolution='provider_not_accepted',
        )

    detail = json.loads(str(error.value))
    assert detail['ok'] is False
    assert detail['error_code'] == 'reconciliation_refused'
    assert 'secret' not in str(error.value)
    assert account.external_id not in str(error.value)
    assert MarketplaceFeedRun.objects.get(pk=uncertain.pk).state == (
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    )


def test_tombstoned_owner_remains_live_only_without_explicit_recovery_option():
    account, listing, uncertain = _uncertain_run('default-tombstone')
    tombstone = timezone.now()
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        is_active=False,
        deleted_at=tombstone,
    )
    Listing.objects.filter(account=account).update(deleted_at=tombstone)

    with pytest.raises(FeedAccountUnavailable, match='inactive or deleted'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_accepted',
            provider_run_id='upload-default-tombstone',
        )
    with pytest.raises(FeedAccountUnavailable, match='inactive or deleted'):
        reconcile_uncertain_feed_run(
            uncertain.pk,
            expected_revision=uncertain.revision,
            resolution='provider_not_accepted',
        )

    assert MarketplaceAccount.all_objects.get(pk=account.pk).deleted_at is not None
    assert Listing.all_objects.get(pk=listing.pk).deleted_at is not None
    assert MarketplaceFeedRun.objects.get(pk=uncertain.pk).state == (
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    )


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='dual_write',
    MARKETPLACE_FEED_RUN_MODE='durable',
)
def test_provider_not_accepted_closes_tombstoned_owner_without_restore_or_flush(
    django_capture_on_commit_callbacks,
):
    account, listing, uncertain = _uncertain_run('tombstone-rejected')
    tombstone = timezone.now()
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        is_active=False,
        deleted_at=tombstone,
    )
    Listing.objects.filter(account=account).update(deleted_at=tombstone)
    account.deleted_at = tombstone

    with patch('apps.marketplaces.tasks.coalesced_flush_task.delay') as flush:
        with django_capture_on_commit_callbacks(execute=True):
            summary = _command(
                uncertain,
                resolution='provider_not_accepted',
                apply=True,
            )

    current_account = MarketplaceAccount.all_objects.get(pk=account.pk)
    current_listing = Listing.all_objects.get(pk=listing.pk)
    current_run = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    dispatch = BackgroundJobDispatch.objects.get(pk=summary['dispatch_id'])
    assert summary['owner_restored'] is False
    assert summary['flush_scheduled'] is False
    assert current_account.deleted_at == tombstone
    assert current_account.is_active is False
    assert current_listing.deleted_at is not None
    assert current_listing.feed_run_id == uncertain.pk
    assert current_run.state == MarketplaceFeedRun.State.FAILED
    assert current_run.revision == uncertain.revision + 1
    assert dispatch.args == [str(uncertain.pk), current_run.revision]
    assert dispatch.deduplication_key == (
        f'feed-run:{uncertain.pk}:rev:{current_run.revision}'
    )
    flush.assert_not_called()


@override_settings(
    AVITO_STATUS_LIFECYCLE_MODE='dual_write',
    MARKETPLACE_FEED_RUN_MODE='durable',
)
def test_provider_not_accepted_closes_inactive_owner_without_reactivation(
    django_capture_on_commit_callbacks,
):
    account, listing, uncertain = _uncertain_run('inactive-rejected')
    MarketplaceAccount.objects.filter(pk=account.pk).update(is_active=False)

    with patch('apps.marketplaces.tasks.coalesced_flush_task.delay') as flush:
        with django_capture_on_commit_callbacks(execute=True):
            summary = _command(
                uncertain,
                resolution='provider_not_accepted',
                apply=True,
            )

    current_account = MarketplaceAccount.objects.get(pk=account.pk)
    current_listing = Listing.objects.get(pk=listing.pk)
    current_run = MarketplaceFeedRun.objects.get(pk=uncertain.pk)
    assert summary['owner_restored'] is False
    assert summary['flush_scheduled'] is False
    assert summary['dispatch_id'] is not None
    assert current_account.is_active is False
    assert current_account.deleted_at is None
    assert current_listing.deleted_at is None
    assert current_listing.feed_run_id == uncertain.pk
    assert current_run.state == MarketplaceFeedRun.State.FAILED
    flush.assert_not_called()
