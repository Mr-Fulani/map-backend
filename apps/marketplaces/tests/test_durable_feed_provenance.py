import datetime
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import pytest
import requests
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.adapters.avito.adapter import FeedItemErrorPage
from apps.marketplaces.feed_workflow import (
    PROVIDER_RESULT_HORIZON,
    claim_due_run_for_account,
    create_or_supersede_feed_run,
    mark_feed_submitted,
    persist_feed_submission_boundary,
    start_reporting,
)
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.marketplaces.tasks import (
    _coalesced_flush_durable,
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
    tenant = Tenant.objects.create(name=f'Provenance {suffix}', slug=f'prov-{suffix}')
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Avito {suffix}',
        external_id=f'account-{suffix}',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'secret'}),
    )


def _listing(account: MarketplaceAccount, suffix: str = 'one') -> Listing:
    product = Product.objects.create(
        tenant=account.tenant,
        article=f'PROV-{account.pk}-{suffix}',
        name=f'Product {suffix}',
        price=Decimal('1000.00'),
    )
    return Listing.objects.create(
        tenant=account.tenant,
        account=account,
        product=product,
        status=Listing.STATUS_PENDING,
        price_on_listing=Decimal('1100.00'),
    )


def _upload(run, upload_id: str, status: str = 'success') -> dict:
    return {
        'upload_id': upload_id,
        'status': status,
        'started_at': (run.submitted_at + datetime.timedelta(seconds=1)).isoformat(),
    }


def _run_claimed_feed_step_for_test(run_id: str, revision: int):
    """Invoke the exact durable leaf used by the generic dispatch wrapper."""

    return process_marketplace_feed_run_step(run_id, revision)


def _submitted_run(
    account: MarketplaceAccount,
    *,
    provider_run_id: str | None = 'upload-current',
    report_completed: bool = False,
):
    transition_at = timezone.now()
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


def test_pre_post_baseline_failure_never_calls_provider_post():
    account = _account('baseline-failure')
    _listing(account)

    with (
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'feed'),
        patch('apps.marketplaces.tasks.AvitoAdapter._upload_to_s3'),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            side_effect=requests.Timeout('baseline unavailable'),
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
    ):
        result = _coalesced_flush_durable(None, account)

    run = MarketplaceFeedRun.objects.get(account=account)
    assert result['status'] == 'pre_submission_retry'
    assert run.state == MarketplaceFeedRun.State.PREPARING
    assert run.submitted_at is None
    assert run.provider_predecessor_run_id is None
    trigger.assert_not_called()


def test_processing_upload_never_reads_global_feed_results(
    django_capture_on_commit_callbacks,
):
    account = _account('processing')
    _listing(account)
    run = _submitted_run(account, report_completed=True)

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value=_upload(run, 'upload-current', status='processing'),
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter.get_feed_results') as results,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.pk),
                run.revision,
            )

    assert result['status'] == 'retry_wait'
    results.assert_not_called()


def test_report_page_pre_post_mismatch_fails_closed_without_mutation(
    django_capture_on_commit_callbacks,
):
    account = _account('report-mismatch')
    listing = _listing(account)
    run = _submitted_run(account)
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        now=timezone.now(),
    )
    reporting = start_reporting(
        claim,
        provider_run_id='upload-current',
        next_attempt_at=timezone.now(),
        now=timezone.now(),
    )

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            side_effect=[
                _upload(reporting, 'upload-current'),
                _upload(reporting, 'upload-next'),
            ],
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_item_error_page',
            return_value=FeedItemErrorPage(
                errors={str(listing.publish_idempotency_key): 'must not apply'},
                next_page=None,
            ),
        ),
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(reporting.pk),
                reporting.revision,
            )

    listing.refresh_from_db()
    current = MarketplaceFeedRun.objects.get(pk=reporting.pk)
    assert result['status'] == 'outcome_uncertain'
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert listing.status == Listing.STATUS_PENDING


def test_id_page_pre_post_mismatch_fails_closed_without_mapping(
    django_capture_on_commit_callbacks,
):
    account = _account('id-mismatch')
    listing = _listing(account)
    run = _submitted_run(account, report_completed=True)

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            side_effect=[
                _upload(run, 'upload-current'),
                _upload(run, 'upload-next'),
            ],
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            return_value=[{
                'ad_id': str(listing.publish_idempotency_key),
                'avito_id': 'remote-1',
            }],
        ),
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.pk),
                run.revision,
            )

    listing.refresh_from_db()
    current = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert result['status'] == 'outcome_uncertain'
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert listing.external_id is None


def test_exact_48_hour_deadline_is_fail_closed_without_provider_read(
    django_capture_on_commit_callbacks,
):
    account = _account('deadline')
    _listing(account)
    run = _submitted_run(account, report_completed=True)
    deadline = run.submitted_at + PROVIDER_RESULT_HORIZON

    with (
        patch('apps.marketplaces.tasks.now', return_value=deadline),
        patch('apps.marketplaces.tasks.AvitoAdapter.get_latest_upload') as latest,
        patch('apps.core.dispatch.publish_dispatch', return_value=False),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = _run_claimed_feed_step_for_test(
                str(run.pk),
                run.revision,
            )

    current = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert result['status'] == 'outcome_uncertain'
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    latest.assert_not_called()
