import datetime
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import pytest
import requests
from django.utils import timezone

from apps.core.models import BackgroundJobDispatch
from apps.datasources.encryption import encrypt
from apps.marketplaces.adapters.avito.adapter import (
    AmbiguousFeedSubmissionError,
    FeedItemErrorPage,
    FeedUploadError,
)
from apps.marketplaces.feed_workflow import (
    claim_due_run_for_account,
    create_or_supersede_feed_run,
    mark_feed_submission_unknown,
    mark_feed_submitted,
    persist_feed_submission_boundary,
    start_reporting,
)
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.marketplaces.tasks import (
    _DURABLE_FEED_UPLOAD_CLOCK_SKEW,
    _coalesced_flush_durable,
    _latest_upload_evidence,
    _mark_feed_submission_boundary,
    dispatch_due_marketplace_feed_runs,
    poll_feed_results_task,
    process_marketplace_feed_run_step,
)
from apps.products.models import Product
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _durable_mode(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'durable'
    settings.AVITO_API_MAX_PAGES = 100


def _account(suffix: str) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Durable {suffix}',
        slug=f'durable-{suffix}'[:50],
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Avito {suffix}',
        external_id=f'avito-account-{suffix}',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'secret'}),
    )


def _listing(account: MarketplaceAccount, suffix: str) -> Listing:
    product = Product.objects.create(
        tenant_id=account.tenant_id,
        article=f'DURABLE-{account.pk}-{suffix}',
        name=f'Durable product {suffix}',
        price=Decimal('1000.00'),
    )
    return Listing.objects.create(
        tenant_id=account.tenant_id,
        account=account,
        product=product,
        status=Listing.STATUS_PENDING,
        price_on_listing=Decimal('1100.00'),
    )


def _run_claimed_feed_step_for_test(run_id: str, revision: int):
    """Invoke the exact durable leaf used by the generic dispatch wrapper."""

    return process_marketplace_feed_run_step(run_id, revision)


def _submitted_run(
    account: MarketplaceAccount,
    *,
    provider_run_id: str | None = 'upload-1',
    transition_at: datetime.datetime | None = None,
    report_completed: bool = False,
):
    transition_at = transition_at or timezone.now()
    run = create_or_supersede_feed_run(account.pk, now=transition_at)
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.run_id,
        expected_revision=run.revision,
        now=transition_at,
    )
    assert claim is not None
    boundary = persist_feed_submission_boundary(
        claim,
        provider_predecessor_run_id='upload-before',
        submitted_at=transition_at,
        now=transition_at,
    )
    assert boundary is not None
    submitted = mark_feed_submitted(
        boundary,
        payload_sha256='a' * 64,
        provider_run_id=provider_run_id,
        submitted_at=transition_at,
        next_attempt_at=transition_at,
        now=transition_at,
    )
    if report_completed:
        MarketplaceFeedRun.objects.filter(pk=submitted.pk).update(
            report_completed_at=transition_at,
        )
        submitted = replace(submitted, report_completed_at=transition_at)
    return submitted


def _submission_unknown_run(
    account: MarketplaceAccount,
    *,
    submitted_at: datetime.datetime,
):
    run = create_or_supersede_feed_run(account.pk, now=submitted_at)
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.run_id,
        expected_revision=run.revision,
        now=submitted_at,
    )
    assert claim is not None
    boundary = persist_feed_submission_boundary(
        claim,
        provider_predecessor_run_id='upload-before',
        submitted_at=submitted_at,
        now=submitted_at,
    )
    assert boundary is not None
    return mark_feed_submission_unknown(
        boundary,
        submitted_at=submitted_at,
        next_attempt_at=submitted_at + datetime.timedelta(seconds=1),
        error='provider response was lost',
        now=submitted_at,
    )


def _latest_upload(run, *, upload_id: str = 'upload-1', status: str = 'success'):
    evidence_floor = run.submitted_at or run.created_at
    return {
        'upload_id': upload_id,
        'status': status,
        'started_at': (evidence_floor + datetime.timedelta(seconds=1)).isoformat(),
    }


def test_latest_upload_evidence_accepts_provider_second_precision():
    account = _account('second-precision')
    transition_at = timezone.now().replace(microsecond=987654)
    run = _submitted_run(account, transition_at=transition_at)
    started_at = transition_at.replace(microsecond=0)

    evidence = _latest_upload_evidence(
        account,
        run,
        {
            'upload_id': 'upload-second-precision',
            'status': 'processing',
            'started_at': started_at.isoformat(),
        },
    )

    assert started_at < transition_at
    assert evidence == ('upload-second-precision', 'processing')


def test_latest_upload_evidence_accepts_at_bounded_clock_skew():
    account = _account('bounded-skew')
    run = _submitted_run(account)
    started_at = run.submitted_at - _DURABLE_FEED_UPLOAD_CLOCK_SKEW

    evidence = _latest_upload_evidence(
        account,
        run,
        {
            'upload_id': 'upload-at-skew-boundary',
            'status': 'success',
            'started_at': started_at.isoformat(),
        },
    )

    assert evidence == ('upload-at-skew-boundary', 'success')


def test_latest_upload_evidence_rejects_older_than_bounded_clock_skew():
    account = _account('outside-bounded-skew')
    run = _submitted_run(account)
    started_at = (
        run.submitted_at
        - _DURABLE_FEED_UPLOAD_CLOCK_SKEW
        - datetime.timedelta(microseconds=1)
    )

    evidence = _latest_upload_evidence(
        account,
        run,
        {
            'upload_id': 'upload-outside-skew-boundary',
            'status': 'success',
            'started_at': started_at.isoformat(),
        },
    )

    assert evidence is None


def test_duplicate_legacy_entry_creates_one_durable_dispatch(
    django_capture_on_commit_callbacks,
):
    account = _account('dedupe')
    _listing(account, 'one')
    run = _submitted_run(account)

    with patch('apps.core.dispatch.publish_dispatch', return_value=False):
        with django_capture_on_commit_callbacks(execute=True):
            first = poll_feed_results_task(account.pk)
            second = poll_feed_results_task(account.pk)

    assert first['dispatch_id'] == second['dispatch_id']
    dispatch = BackgroundJobDispatch.objects.get()
    assert dispatch.deduplication_key == f'feed-run:{run.run_id}:rev:{run.revision}'
    assert dispatch.max_run_attempts == 25
    assert dispatch.execution_timeout_seconds == 180


def test_submission_boundary_is_persisted_before_provider_post():
    account = _account('submission-boundary')
    _listing(account, 'one')
    transition_at = timezone.now()
    run = create_or_supersede_feed_run(account.pk, now=transition_at)
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.run_id,
        expected_revision=run.revision,
        now=transition_at,
    )
    assert claim is not None

    boundary_claim = _mark_feed_submission_boundary(
        claim,
        provider_predecessor_run_id='upload-before',
        submitted_at=transition_at,
    )

    current = MarketplaceFeedRun.objects.get(pk=run.run_id)
    assert boundary_claim.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert boundary_claim.submitted_at == transition_at
    assert boundary_claim.revision == claim.revision + 1
    assert current.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert current.submitted_at == transition_at
    assert current.provider_predecessor_run_id == 'upload-before'
    assert current.provider_result_deadline_at == (
        transition_at + datetime.timedelta(hours=48)
    )
    assert current.claim_token == claim.claim_token


@pytest.mark.parametrize(
    ('owner_change', 'expected_state'),
    (
        ('inactive', MarketplaceFeedRun.State.CANCELLED),
        ('identity', MarketplaceFeedRun.State.SUPERSEDED),
    ),
)
def test_owner_is_revalidated_after_s3_and_before_provider_post(
    owner_change,
    expected_state,
):
    account = _account(f'pre-post-{owner_change}')
    _listing(account, 'one')

    def mutate_owner(_payload):
        updates = (
            {'is_active': False}
            if owner_change == 'inactive'
            else {'external_id': f'replaced-{account.external_id}'}
        )
        MarketplaceAccount.all_objects.filter(pk=account.pk).update(**updates)

    with (
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'feed'),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter._upload_to_s3',
            side_effect=mutate_owner,
        ) as upload,
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
    ):
        result = _coalesced_flush_durable(None, account)

    current = MarketplaceFeedRun.objects.get(account_id=account.pk)
    assert result == {
        'status': 'stale_before_submission',
        'run_id': str(current.pk),
    }
    assert current.state == expected_state
    assert current.submitted_at is None
    assert current.finished_at is not None
    upload.assert_called_once_with(b'feed')
    trigger.assert_not_called()


def test_preparing_recovery_fails_before_boundary_without_provider_call(
    django_capture_on_commit_callbacks,
):
    account = _account('preparing-recovery')
    _listing(account, 'one')
    run = create_or_supersede_feed_run(account.pk, now=timezone.now())

    with (
        patch('apps.marketplaces.tasks.AvitoAdapter.get_latest_upload') as latest,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.run_id),
                run.revision,
            )

    current = MarketplaceFeedRun.objects.get(pk=run.run_id)
    assert result['status'] == 'failed_pre_submission'
    assert current.state == MarketplaceFeedRun.State.FAILED
    assert current.submitted_at is None
    latest.assert_not_called()


def test_submission_unknown_has_bounded_negative_reconciliation_then_manual_failure(
    django_capture_on_commit_callbacks,
):
    account = _account('ambiguous-negative-horizon')
    _listing(account, 'one')
    submitted_at = timezone.now()
    current = _submission_unknown_run(account, submitted_at=submitted_at)
    negative_read_times = [
        submitted_at + datetime.timedelta(minutes=minute)
        for minute in (1, 31, 61, 91)
    ]

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={},
        ) as latest,
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            for read_at in negative_read_times:
                with patch('apps.marketplaces.tasks.now', return_value=read_at):
                    result = _run_claimed_feed_step_for_test(
                        str(current.pk),
                        current.revision,
                    )
                current = MarketplaceFeedRun.objects.get(pk=current.pk)
                assert result['status'] == 'retry_wait'
                assert current.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN

            assert current.submission_reconcile_attempt == 4
            terminal_at = submitted_at + datetime.timedelta(minutes=121)
            with patch('apps.marketplaces.tasks.now', return_value=terminal_at):
                result = _run_claimed_feed_step_for_test(
                    str(current.pk),
                    current.revision,
                )

    current.refresh_from_db()
    assert result['status'] == 'outcome_uncertain'
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.submission_reconcile_attempt == 5
    assert 'outcome_uncertain' in current.last_error
    assert 'manual reconciliation' in current.last_error
    assert latest.call_count == 5
    assert all(call.kwargs == {'strict': True} for call in latest.call_args_list)
    trigger.assert_not_called()
    assert BackgroundJobDispatch.objects.filter(
        deduplication_key=f'feed-run:{current.pk}:rev:{current.revision}',
    ).count() == 1

    with (
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'blocked-feed'),
        patch('apps.marketplaces.tasks.AvitoAdapter._upload_to_s3') as upload,
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as replay_post,
        patch('apps.marketplaces.tasks.coalesced_flush_task.apply_async') as reschedule,
    ):
        blocked = _coalesced_flush_durable(None, account)

    assert blocked == {'status': 'manual_reconciliation_required'}
    upload.assert_not_called()
    replay_post.assert_not_called()
    reschedule.assert_not_called()


def test_submission_unknown_network_error_does_not_count_negative_or_retry_post(
    django_capture_on_commit_callbacks,
):
    account = _account('ambiguous-network-error')
    _listing(account, 'one')
    submitted_at = timezone.now()
    run = _submission_unknown_run(account, submitted_at=submitted_at)
    retry_at = submitted_at + datetime.timedelta(minutes=1)

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            side_effect=requests.Timeout('provider outage'),
        ) as latest,
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
        patch('apps.marketplaces.tasks.now', return_value=retry_at),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.run_id),
                run.revision,
            )

    current = MarketplaceFeedRun.objects.get(pk=run.run_id)
    assert result['status'] == 'retry_wait'
    assert current.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert current.submission_reconcile_attempt == 0
    latest.assert_called_once_with(strict=True)
    trigger.assert_not_called()


@pytest.mark.parametrize(
    'submission_error',
    (
        AmbiguousFeedSubmissionError('HTTP 503 may have crossed the boundary'),
        requests.Timeout('provider response timed out'),
    ),
)
def test_ambiguous_provider_post_enters_reconciliation_without_retry(
    submission_error,
    django_capture_on_commit_callbacks,
):
    account = _account(f'ambiguous-post-{type(submission_error).__name__.lower()}')
    _listing(account, 'one')

    with (
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'feed'),
        patch('apps.marketplaces.tasks.AvitoAdapter._upload_to_s3') as upload,
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={},
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter._trigger_autoload',
            side_effect=submission_error,
        ) as trigger,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _coalesced_flush_durable(None, account)

    current = MarketplaceFeedRun.objects.get(account=account)
    assert result == {'status': 'submission_unknown', 'run_id': str(current.pk)}
    assert current.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert current.submitted_at is not None
    assert current.next_attempt_at is not None
    assert current.finished_at is None
    upload.assert_called_once_with(b'feed')
    trigger.assert_called_once_with()
    assert BackgroundJobDispatch.objects.filter(
        deduplication_key=f'feed-run:{current.pk}:rev:{current.revision}',
    ).count() == 1


def test_explicit_safe_provider_rejection_fails_without_reconciliation(
    django_capture_on_commit_callbacks,
):
    account = _account('safe-post-rejection')
    _listing(account, 'one')

    with (
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'feed'),
        patch('apps.marketplaces.tasks.AvitoAdapter._upload_to_s3'),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={},
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter._trigger_autoload',
            side_effect=FeedUploadError('HTTP 422 rejected before processing'),
        ),
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _coalesced_flush_durable(None, account)

    current = MarketplaceFeedRun.objects.get(account=account)
    assert result == {'status': 'failed', 'run_id': str(current.pk)}
    assert current.state == MarketplaceFeedRun.State.FAILED
    assert current.finished_at is not None


def test_active_submitted_run_prevents_feed_object_overwrite_and_reschedules():
    account = _account('active-owner')
    _listing(account, 'one')
    _submitted_run(account)

    with (
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'new-feed'),
        patch('apps.marketplaces.tasks.AvitoAdapter._upload_to_s3') as upload,
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
        patch('apps.marketplaces.tasks.cache.add', return_value=True),
        patch('apps.marketplaces.tasks.coalesced_flush_task.apply_async') as reschedule,
    ):
        result = _coalesced_flush_durable(None, account)

    assert result == {'status': 'active_feed_run'}
    upload.assert_not_called()
    trigger.assert_not_called()
    reschedule.assert_called_once()


def test_exact_revision_performs_one_poll_http_and_stale_duplicate_is_noop(
    django_capture_on_commit_callbacks,
):
    account = _account('exact-revision')
    listing = _listing(account, 'one')
    run = _submitted_run(account, report_completed=True)
    result_item = {
        'ad_id': str(listing.publish_idempotency_key),
        'avito_id': 12345,
    }

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value=_latest_upload(run),
        ) as latest,
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            return_value=[result_item],
        ) as get_results,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            first = _run_claimed_feed_step_for_test(
                str(run.run_id),
                run.revision,
            )
            duplicate = _run_claimed_feed_step_for_test(
                str(run.run_id),
                run.revision,
            )

    listing.refresh_from_db()
    current = MarketplaceFeedRun.objects.get(pk=run.run_id)
    assert first['status'] == 'poll_page_applied'
    assert duplicate == {'status': 'stale'}
    assert latest.call_count == 2
    assert get_results.call_count == 1
    assert listing.status == Listing.STATUS_ACTIVE
    assert listing.external_id == '12345'
    assert BackgroundJobDispatch.objects.filter(
        deduplication_key=f'feed-run:{run.run_id}:rev:{current.revision}',
    ).exists()


def test_poll_page_is_hard_capped_at_100_rows(django_capture_on_commit_callbacks):
    account = _account('batch-cap')
    listings = [_listing(account, f'{index:03}') for index in range(101)]
    run = _submitted_run(account, report_completed=True)

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value=_latest_upload(run),
        ) as latest,
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            return_value=[],
        ) as get_results,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.run_id),
                run.revision,
            )

    current = MarketplaceFeedRun.objects.get(pk=run.run_id)
    assert result['page_size'] == 100
    assert len(get_results.call_args.args[0]) == 100
    assert latest.call_count == 2
    assert current.poll_cursor_listing_id == listings[99].pk
    assert Listing.objects.filter(feed_run_id=run.run_id, status=Listing.STATUS_PENDING).count() == 101


def test_reporting_rechecks_exact_upload_and_old_report_cannot_reject(
    django_capture_on_commit_callbacks,
):
    account = _account('old-report')
    listing = _listing(account, 'one')
    run = _submitted_run(account, provider_run_id='upload-current')
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.run_id,
        expected_revision=run.revision,
        now=timezone.now(),
    )
    assert claim is not None
    reporting = start_reporting(
        claim,
        provider_run_id='upload-current',
        next_attempt_at=timezone.now(),
        now=timezone.now(),
    )

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value=_latest_upload(reporting, upload_id='upload-old'),
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_item_error_page',
            return_value=FeedItemErrorPage(
                errors={str(listing.publish_idempotency_key): 'old error'},
                next_page=None,
            ),
        ) as get_report,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(reporting.run_id),
                reporting.revision,
            )

    listing.refresh_from_db()
    current = MarketplaceFeedRun.objects.get(pk=reporting.run_id)
    assert result['status'] == 'outcome_uncertain'
    assert listing.status == Listing.STATUS_PENDING
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.report_attempt == 0
    get_report.assert_not_called()


def test_upload_older_than_clock_skew_is_not_valid_evidence(
    django_capture_on_commit_callbacks,
):
    account = _account('pre-post-upload')
    _listing(account, 'one')
    run = _submitted_run(account, provider_run_id=None)
    assert run.submitted_at is not None
    old_started_at = (
        run.submitted_at
        - _DURABLE_FEED_UPLOAD_CLOCK_SKEW
        - datetime.timedelta(microseconds=1)
    )

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={
                'upload_id': 'upload-before-post',
                'status': 'success',
                'started_at': old_started_at.isoformat(),
            },
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter.get_feed_results') as get_results,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.run_id),
                run.revision,
            )

    current = MarketplaceFeedRun.objects.get(pk=run.run_id)
    assert result['status'] == 'retry_wait'
    assert current.provider_run_id is None
    get_results.assert_not_called()


def test_report_terminal_transition_persists_one_durable_digest_leaf(
    django_capture_on_commit_callbacks,
):
    account = _account('digest')
    listing = _listing(account, 'one')
    run = _submitted_run(account)
    Listing.objects.filter(pk=listing.pk).update(
        status=Listing.STATUS_ACTIVE,
        external_id='remote-digest',
    )
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.run_id,
        expected_revision=run.revision,
        now=timezone.now(),
    )
    assert claim is not None
    reporting = start_reporting(
        claim,
        provider_run_id='upload-1',
        next_attempt_at=timezone.now(),
        now=timezone.now(),
    )

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            side_effect=[
                _latest_upload(reporting),
                _latest_upload(reporting),
            ],
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_item_error_page',
            return_value=FeedItemErrorPage(errors={}, next_page=None),
        ),
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(reporting.run_id),
                reporting.revision,
            )

    current = MarketplaceFeedRun.objects.get(pk=reporting.run_id)
    assert result['status'] == 'completed'
    assert current.state == MarketplaceFeedRun.State.SUCCEEDED
    digest_dispatches = BackgroundJobDispatch.objects.filter(
        deduplication_key=f'feed-run:{current.pk}:rev:{current.revision}',
    )
    assert digest_dispatches.count() == 1


def test_recovery_broker_failure_leaves_one_pending_dispatch(
    django_capture_on_commit_callbacks,
):
    account = _account('recovery')
    _listing(account, 'one')
    run = _submitted_run(account)

    with patch('apps.core.dispatch.publish_dispatch', return_value=False):
        with django_capture_on_commit_callbacks(execute=True):
            first = dispatch_due_marketplace_feed_runs()
            second = dispatch_due_marketplace_feed_runs()

    assert first['selected'] == second['selected'] == 1
    dispatch = BackgroundJobDispatch.objects.get(
        deduplication_key=f'feed-run:{run.run_id}:rev:{run.revision}',
    )
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert BackgroundJobDispatch.objects.count() == 1
