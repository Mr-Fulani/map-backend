from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone

import apps.marketplaces.feed_workflow as feed_workflow
from apps.core.retention import purge_retained_data
from apps.datasources.encryption import encrypt
from apps.marketplaces.feed_workflow import (
    FeedAccountUnavailable,
    FeedRunConflict,
    FeedSubmissionOutcomeUncertain,
    StaleFeedRunClaim,
    account_identity_digest,
    advance_report_page,
    apply_poll_page,
    apply_report_page,
    cancel_feed_runs_for_inactive_owners,
    claim_due_run_for_account,
    claim_due_runs,
    complete_poll_step,
    create_or_supersede_feed_run,
    fence_account_feed_runs_for_owner_change,
    finish_feed_run,
    load_poll_batch,
    mark_feed_submitted,
    mark_feed_submission_unknown,
    OWNER_CHANGE_HOLD_SUBMITTED,
    record_provider_run_observation,
    reset_poll_round,
    retry_step,
    start_reporting,
)
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.products.models import Product
from apps.sync.models import SyncLog
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


def _account(suffix: str) -> MarketplaceAccount:
    tenant = Tenant.objects.create(name=f'Feed {suffix}', slug=f'feed-{suffix}')
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


def _listing(
    account: MarketplaceAccount,
    suffix: str,
    *,
    status: str = Listing.STATUS_PENDING,
    external_id: str | None = None,
) -> Listing:
    product = Product.objects.create(
        tenant_id=account.tenant_id,
        article=f'FEED-{account.pk}-{suffix}',
        name=f'Feed product {suffix}',
        price=Decimal('1000.00'),
    )
    return Listing.objects.create(
        tenant_id=account.tenant_id,
        account=account,
        product=product,
        status=status,
        external_id=external_id,
        price_on_listing=Decimal('1100.00'),
    )


def _submitted_run(account: MarketplaceAccount, now):
    run = create_or_supersede_feed_run(account.pk, now=now)
    preparing_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        now=now,
    )
    assert preparing_claim is not None
    return mark_feed_submitted(
        preparing_claim,
        payload_sha256='b' * 64,
        provider_run_id='upload-1',
        submitted_at=now,
        next_attempt_at=now,
        now=now,
    )


def test_account_identity_is_stable_across_semantic_credential_reencryption():
    account = _account('credential-reencryption')
    original_ciphertext = bytes(account.credentials_enc)
    original_digest = account_identity_digest(account)

    account.credentials_enc = encrypt({
        # Reverse insertion order to prove canonical JSON, not serialized
        # dict order or randomized Fernet bytes, owns the identity.
        'client_secret': 'secret',
        'client_id': 'client-credential-reencryption',
    })
    account.save(update_fields=['credentials_enc'])
    reencryption_digest = account_identity_digest(account)

    assert bytes(account.credentials_enc) != original_ciphertext
    assert reencryption_digest == original_digest

    account.credentials_enc = encrypt({
        'client_id': 'client-credential-reencryption',
        'client_secret': 'rotated-secret',
    })
    account.save(update_fields=['credentials_enc'])

    assert account_identity_digest(account) != original_digest


def test_opaque_legacy_credentials_have_deterministic_fail_closed_identity():
    account = _account('opaque-credential-fallback')
    account.credentials_enc = b'opaque-legacy-value'
    first_digest = account_identity_digest(account)

    assert account_identity_digest(account) == first_digest

    account.credentials_enc = b'changed-opaque-legacy-value'
    assert account_identity_digest(account) != first_digest


def test_create_is_idempotent_and_tags_only_current_pending_rows():
    now = timezone.now()
    account = _account('create')
    pending = _listing(account, 'pending')
    active = _listing(account, 'active', status=Listing.STATUS_ACTIVE, external_id='remote-active')
    generation_id = uuid4()

    created = create_or_supersede_feed_run(
        account.pk,
        generation_id=generation_id,
        payload_sha256='a' * 64,
        now=now,
    )
    replayed = create_or_supersede_feed_run(
        account.pk,
        generation_id=generation_id,
        payload_sha256='a' * 64,
        now=now + timedelta(seconds=1),
    )

    pending.refresh_from_db()
    active.refresh_from_db()
    assert created == replayed
    assert created.run_id == generation_id
    assert created.total_count == 1
    assert created.pending_count == 1
    assert pending.feed_run_id == generation_id
    assert active.feed_run_id is None
    assert MarketplaceFeedRun.objects.filter(account=account).count() == 1


def test_new_generation_supersedes_old_owner_and_retags_pending_rows():
    now = timezone.now()
    account = _account('supersede')
    listing = _listing(account, 'pending')
    first = create_or_supersede_feed_run(account.pk, now=now)

    second = create_or_supersede_feed_run(account.pk, now=now + timedelta(seconds=1))

    listing.refresh_from_db()
    old = MarketplaceFeedRun.objects.get(pk=first.pk)
    assert old.state == MarketplaceFeedRun.State.SUPERSEDED
    assert old.finished_at == now + timedelta(seconds=1)
    assert old.revision == 1
    assert listing.feed_run_id == second.pk
    assert second.total_count == 1


def test_generation_over_10k_refuses_atomically_without_superseding_or_retagging(monkeypatch):
    now = timezone.now()
    account = _account('generation-limit')
    first = _listing(account, 'first')
    existing = create_or_supersede_feed_run(account.pk, now=now)
    second = _listing(account, 'second')
    monkeypatch.setattr(feed_workflow, 'MAX_GENERATION_LISTINGS', 1)

    with pytest.raises(FeedRunConflict, match='cannot contain more than 1'):
        create_or_supersede_feed_run(account.pk, now=now + timedelta(seconds=1))

    existing_row = MarketplaceFeedRun.objects.get(pk=existing.pk)
    first.refresh_from_db()
    second.refresh_from_db()
    assert existing_row.state == MarketplaceFeedRun.State.PREPARING
    assert existing_row.revision == existing.revision
    assert MarketplaceFeedRun.objects.filter(account=account).count() == 1
    assert first.feed_run_id == existing.pk
    assert second.feed_run_id is None


def test_new_generation_cannot_supersede_claimed_or_submitted_owner():
    now = timezone.now()
    account = _account('unsafe-supersede')
    listing = _listing(account, 'pending')
    run = create_or_supersede_feed_run(account.pk, now=now)
    claim = claim_due_run_for_account(account.pk, now=now)

    with pytest.raises(FeedRunConflict, match='provider boundary'):
        create_or_supersede_feed_run(account.pk, now=now + timedelta(seconds=1))

    submitted = mark_feed_submitted(
        claim,
        payload_sha256='c' * 64,
        provider_run_id='upload-owned',
        submitted_at=now,
        next_attempt_at=now + timedelta(seconds=2),
        now=now + timedelta(seconds=1),
    )
    with pytest.raises(FeedRunConflict, match='provider boundary'):
        create_or_supersede_feed_run(account.pk, now=now + timedelta(seconds=3))

    listing.refresh_from_db()
    assert listing.feed_run_id == run.pk
    assert MarketplaceFeedRun.objects.get(pk=run.pk).state == submitted.state
    assert MarketplaceFeedRun.objects.filter(account=account).count() == 1


def test_uncertain_account_owner_blocks_new_generation_without_listing_membership():
    transition_at = timezone.now()
    account = _account('uncertain-account-owner')
    listing = _listing(account, 'pending')
    run = create_or_supersede_feed_run(account.pk, now=transition_at)
    MarketplaceFeedRun.objects.filter(pk=run.pk).update(
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        next_attempt_at=None,
        finished_at=transition_at,
    )
    Listing.objects.filter(pk=listing.pk).update(feed_run_id=None)

    with pytest.raises(FeedSubmissionOutcomeUncertain, match='owns this account'):
        create_or_supersede_feed_run(
            account.pk,
            now=transition_at + timedelta(seconds=1),
        )

    assert MarketplaceFeedRun.objects.filter(account=account).count() == 1


def test_owner_change_api_closes_only_proven_preparing_owner_safely():
    transition_at = timezone.now()
    account = _account('owner-change-preparing')
    _listing(account, 'pending')
    run = create_or_supersede_feed_run(account.pk, now=transition_at)

    fenced = fence_account_feed_runs_for_owner_change(
        account.pk,
        reason='Credentials will change.',
        now=transition_at + timedelta(seconds=1),
    )

    current = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert fenced is not None
    assert fenced.state == MarketplaceFeedRun.State.SUPERSEDED
    assert current.state == MarketplaceFeedRun.State.SUPERSEDED


def test_owner_change_api_blocks_submitted_identity_mutation():
    transition_at = timezone.now()
    account = _account('owner-change-block-submitted')
    _listing(account, 'pending')
    submitted = _submitted_run(account, transition_at)

    with pytest.raises(FeedSubmissionOutcomeUncertain, match='owns this account'):
        fence_account_feed_runs_for_owner_change(
            account.pk,
            reason='Credentials will change.',
            now=transition_at + timedelta(seconds=1),
        )

    current = MarketplaceFeedRun.objects.get(pk=submitted.pk)
    assert current.state == MarketplaceFeedRun.State.POLLING
    assert current.revision == submitted.revision


def test_owner_change_api_can_hold_submitted_owner_for_deactivation():
    transition_at = timezone.now()
    account = _account('owner-change-hold-submitted')
    _listing(account, 'pending')
    submitted = _submitted_run(account, transition_at)

    fenced = fence_account_feed_runs_for_owner_change(
        account.pk,
        reason='Marketplace account will be deactivated.',
        safe_state=MarketplaceFeedRun.State.CANCELLED,
        submitted_policy=OWNER_CHANGE_HOLD_SUBMITTED,
        now=transition_at + timedelta(seconds=1),
    )

    current = MarketplaceFeedRun.objects.get(pk=submitted.pk)
    assert fenced is not None
    assert fenced.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.finished_at is not None


def test_account_delete_holds_unknown_post_and_retention_keeps_owner(settings):
    settings.SOFT_DELETE_RETENTION_DAYS = 1
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    transition_at = timezone.now()
    account = _account('delete-hold-unknown-post')
    run = MarketplaceFeedRun.objects.create(
        tenant_id=account.tenant_id,
        account=account,
        marketplace=account.marketplace,
        state=MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        account_identity_digest=account_identity_digest(account),
        submitted_at=transition_at,
        next_attempt_at=transition_at + timedelta(minutes=1),
    )

    account.soft_delete()
    expired = transition_at - timedelta(days=2)
    MarketplaceAccount.all_objects.filter(pk=account.pk).update(
        deleted_at=expired,
    )
    result = purge_retained_data()

    run.refresh_from_db()
    assert run.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert run.finished_at is not None
    assert run.next_attempt_at is None
    assert result['marketplace_accounts'] == 0
    assert MarketplaceAccount.all_objects.filter(pk=account.pk).exists()


def test_claim_is_single_flight_and_expired_lease_can_be_taken_over():
    now = timezone.now()
    account = _account('lease')
    run = create_or_supersede_feed_run(account.pk, now=now)

    first = claim_due_run_for_account(account.pk, now=now, lease=timedelta(minutes=2))
    duplicate = claim_due_run_for_account(account.pk, now=now + timedelta(minutes=1))
    takeover = claim_due_run_for_account(account.pk, now=now + timedelta(minutes=3))

    assert first is not None
    assert first.revision == run.revision + 1
    assert duplicate is None
    assert takeover is not None
    assert takeover.claim_token != first.claim_token
    assert takeover.revision == first.revision + 1
    with pytest.raises((FeedRunConflict, StaleFeedRunClaim)):
        retry_step(
            first,
            error='late result',
            next_attempt_at=now + timedelta(minutes=10),
            now=now + timedelta(minutes=3),
        )


def test_claim_requires_exact_expected_revision_and_identity_is_fenced():
    now = timezone.now()
    account = _account('identity')
    run = create_or_supersede_feed_run(account.pk, now=now)

    assert claim_due_run_for_account(
        account.pk,
        expected_revision=run.revision + 1,
        now=now,
    ) is None

    MarketplaceAccount.objects.filter(pk=account.pk).update(external_id='replacement-account')
    assert claim_due_run_for_account(account.pk, now=now) is None
    run_row = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert run_row.state == MarketplaceFeedRun.State.SUPERSEDED
    assert run_row.finished_at == now


def test_inactive_tenant_is_fenced_from_create_claim_recovery_and_claimed_reads():
    now = timezone.now()
    account = _account('inactive-tenant')
    _listing(account, 'pending')
    run = create_or_supersede_feed_run(account.pk, now=now)
    claim = claim_due_run_for_account(account.pk, now=now)
    account.tenant.is_active = False
    account.tenant.save(update_fields=['is_active'])

    with pytest.raises(StaleFeedRunClaim):
        load_poll_batch(claim, now=now)
    assert claim_due_run_for_account(account.pk, now=now) is None
    assert claim_due_runs(now=now) == ()

    other = _account('inactive-tenant-create')
    other.tenant.is_active = False
    other.tenant.save(update_fields=['is_active'])
    with pytest.raises(FeedAccountUnavailable):
        create_or_supersede_feed_run(other.pk, now=now)

    run_row = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert run_row.state == MarketplaceFeedRun.State.CANCELLED


def test_recovery_claims_bounded_due_runs_and_skips_future_work():
    now = timezone.now()
    due_account = _account('recovery-due')
    future_account = _account('recovery-future')
    create_or_supersede_feed_run(due_account.pk, now=now)
    future = create_or_supersede_feed_run(future_account.pk, now=now)
    MarketplaceFeedRun.objects.filter(pk=future.pk).update(next_attempt_at=now + timedelta(hours=1))

    claims = claim_due_runs(limit=1, marketplace=due_account.marketplace, now=now)

    assert len(claims) == 1
    assert claims[0].account_id == due_account.pk


def test_inactive_owner_cleanup_is_bounded_revalidated_and_revokes_claims():
    now = timezone.now()
    inactive_account = _account('cleanup-inactive-account')
    deleted_account = _account('cleanup-deleted-account')
    inactive_tenant_account = _account('cleanup-inactive-tenant')
    live_account = _account('cleanup-live')
    runs = [
        create_or_supersede_feed_run(account.pk, now=now)
        for account in (
            inactive_account,
            deleted_account,
            inactive_tenant_account,
            live_account,
        )
    ]
    claimed = claim_due_run_for_account(inactive_account.pk, now=now)
    MarketplaceAccount.all_objects.filter(pk=inactive_account.pk).update(is_active=False)
    MarketplaceAccount.all_objects.filter(pk=deleted_account.pk).update(
        is_active=False,
        deleted_at=now,
    )
    inactive_tenant_account.tenant.is_active = False
    inactive_tenant_account.tenant.save(update_fields=['is_active'])

    first_batch = cancel_feed_runs_for_inactive_owners(limit=2, now=now + timedelta(seconds=1))
    second_batch = cancel_feed_runs_for_inactive_owners(limit=2, now=now + timedelta(seconds=2))

    assert len(first_batch) == 2
    assert len(second_batch) == 1
    assert {snapshot.run_id for snapshot in (*first_batch, *second_batch)} == {
        runs[0].run_id,
        runs[1].run_id,
        runs[2].run_id,
    }
    rows = {row.pk: row for row in MarketplaceFeedRun.objects.filter(pk__in=[run.pk for run in runs])}
    for orphan in runs[:3]:
        row = rows[orphan.pk]
        assert row.state == MarketplaceFeedRun.State.CANCELLED
        assert row.finished_at is not None
        assert row.claim_token is None
        assert row.claimed_until is None
    assert claimed is not None
    assert rows[runs[0].pk].revision > claimed.revision
    assert rows[runs[3].pk].state == MarketplaceFeedRun.State.PREPARING


def test_submitted_inactive_owner_cleanup_retains_uncertain_account_hold():
    transition_at = timezone.now()
    account = _account('cleanup-submitted-owner')
    _listing(account, 'pending')
    submitted = _submitted_run(account, transition_at)
    MarketplaceAccount.all_objects.filter(pk=account.pk).update(is_active=False)

    fenced = cancel_feed_runs_for_inactive_owners(
        limit=1,
        now=transition_at + timedelta(seconds=1),
    )

    current = MarketplaceFeedRun.objects.get(pk=submitted.pk)
    assert len(fenced) == 1
    assert fenced[0].state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.finished_at is not None
    assert 'manual reconciliation' in current.last_error


def test_submitted_identity_change_claim_retains_uncertain_account_hold():
    transition_at = timezone.now()
    account = _account('claim-submitted-identity')
    _listing(account, 'pending')
    submitted = _submitted_run(account, transition_at)
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        external_id='replacement-provider-account',
    )

    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=submitted.pk,
        expected_revision=submitted.revision,
        now=transition_at + timedelta(seconds=1),
    )

    current = MarketplaceFeedRun.objects.get(pk=submitted.pk)
    assert claim is None
    assert current.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
    assert current.finished_at is not None
    assert 'manual reconciliation' in current.last_error


@pytest.mark.parametrize('limit', [True, 0, 101])
def test_inactive_owner_cleanup_rejects_unbounded_limits(limit):
    with pytest.raises(ValueError, match='between 1 and 100'):
        cancel_feed_runs_for_inactive_owners(limit=limit)


def test_poll_keyset_is_capped_at_100_and_stale_revision_cannot_advance():
    now = timezone.now()
    account = _account('keyset')
    for index in range(105):
        _listing(account, f'{index:03}')
    submitted = _submitted_run(account, now)
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=submitted.pk,
        expected_revision=submitted.revision,
        now=now,
    )
    assert claim is not None

    first_page = load_poll_batch(claim, limit=100, now=now)
    advanced = complete_poll_step(
        claim,
        last_listing_id=first_page[-1].pk,
        next_attempt_at=now,
        now=now,
    )

    assert len(first_page) == 100
    assert advanced.poll_cursor_listing_id == first_page[-1].pk
    assert advanced.revision == claim.revision + 1
    with pytest.raises(StaleFeedRunClaim):
        complete_poll_step(
            claim,
            last_listing_id=first_page[-1].pk,
            next_attempt_at=now,
            now=now,
        )

    next_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=advanced.revision,
        now=now,
    )
    assert next_claim is not None
    second_page = load_poll_batch(next_claim, now=now)
    assert len(second_page) == 5
    assert all(row.pk > first_page[-1].pk for row in second_page)


def test_poll_round_reset_refuses_to_skip_rows_then_resets_at_tail():
    now = timezone.now()
    account = _account('round')
    first = _listing(account, 'first')
    second = _listing(account, 'second')
    submitted = _submitted_run(account, now)
    claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)
    assert claim is not None

    with pytest.raises(FeedRunConflict):
        reset_poll_round(
            claim,
            next_attempt_at=now + timedelta(minutes=30),
            now=now,
        )

    advanced = complete_poll_step(
        claim,
        last_listing_id=max(first.pk, second.pk),
        next_attempt_at=now,
        now=now,
    )
    tail_claim = claim_due_run_for_account(account.pk, expected_revision=advanced.revision, now=now)
    assert tail_claim is not None
    reset = reset_poll_round(
        tail_claim,
        next_attempt_at=now + timedelta(minutes=30),
        now=now,
    )
    assert reset.poll_cursor_listing_id == 0
    assert reset.poll_round == 1
    assert reset.pending_count == 2


def test_reporting_page_and_finish_are_exactly_fenced():
    now = timezone.now()
    account = _account('report')
    _listing(account, 'pending')
    submitted = _submitted_run(account, now)
    poll_claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)
    assert poll_claim is not None
    reporting = start_reporting(
        poll_claim,
        provider_run_id='upload-1',
        next_attempt_at=now,
        now=now,
    )

    report_claim = claim_due_run_for_account(account.pk, expected_revision=reporting.revision, now=now)
    assert report_claim is not None
    page_two = advance_report_page(
        report_claim,
        current_page=1,
        next_attempt_at=now,
        now=now,
    )
    assert page_two.report_page == 2
    assert page_two.report_attempt == 0

    with pytest.raises(StaleFeedRunClaim):
        advance_report_page(
            report_claim,
            current_page=1,
            next_attempt_at=now,
            now=now,
        )

    finish_claim = claim_due_run_for_account(account.pk, expected_revision=page_two.revision, now=now)
    assert finish_claim is not None
    with pytest.raises(FeedRunConflict, match='pending listings cannot succeed'):
        finish_feed_run(finish_claim, now=now)
    finished = finish_feed_run(
        finish_claim,
        state=MarketplaceFeedRun.State.FAILED,
        error='operator stopped incomplete report',
        now=now,
    )
    assert finished.state == MarketplaceFeedRun.State.FAILED
    assert finished.finished_at == now
    assert finished.next_attempt_at is None


def test_provider_run_observation_binds_ambiguous_submission_and_is_fenced():
    now = timezone.now()
    account = _account('provider-observation')
    run = create_or_supersede_feed_run(account.pk, now=now)
    preparing_claim = claim_due_run_for_account(account.pk, now=now)
    unknown = mark_feed_submission_unknown(
        preparing_claim,
        submitted_at=now,
        error='provider response was lost',
        next_attempt_at=now + timedelta(seconds=1),
        now=now,
    )
    unknown_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=unknown.revision,
        now=now + timedelta(seconds=1),
    )

    observed = record_provider_run_observation(
        unknown_claim,
        provider_run_id=' provider-upload-42 ',
        next_attempt_at=now + timedelta(seconds=2),
        now=now + timedelta(seconds=1),
    )

    assert observed.state == MarketplaceFeedRun.State.POLLING
    assert observed.provider_run_id == 'provider-upload-42'
    assert observed.payload_sha256 == ''
    assert observed.submitted_at == now
    assert observed.created_at is not None
    assert observed.revision == unknown_claim.revision + 1
    with pytest.raises(StaleFeedRunClaim):
        record_provider_run_observation(
            unknown_claim,
            provider_run_id='provider-upload-42',
            next_attempt_at=now + timedelta(seconds=3),
            now=now + timedelta(seconds=2),
        )

    polling_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=observed.revision,
        now=now + timedelta(seconds=2),
    )
    with pytest.raises(FeedRunConflict):
        record_provider_run_observation(
            polling_claim,
            provider_run_id='different-upload',
            next_attempt_at=now + timedelta(seconds=3),
            now=now + timedelta(seconds=2),
        )


def test_provider_observation_requires_ambiguous_submission_timestamp():
    now = timezone.now()
    account = _account('provider-observation-no-submission')
    run = create_or_supersede_feed_run(account.pk, now=now)
    first_claim = claim_due_run_for_account(account.pk, now=now)
    assert first_claim is not None
    # Simulate a legacy/corrupt SUBMIT_UNKNOWN row written before submitted_at
    # became mandatory, then acquire a fresh exact revision claim.
    MarketplaceFeedRun.objects.filter(pk=run.pk).update(
        state=MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        revision=first_claim.revision + 1,
        claim_token=None,
        claimed_until=None,
    )
    claim = claim_due_run_for_account(account.pk, now=now)
    assert claim is not None

    with pytest.raises(FeedRunConflict, match='without an ambiguous submission timestamp'):
        record_provider_run_observation(
            claim,
            provider_run_id='unbound-upload',
            next_attempt_at=now + timedelta(seconds=1),
            now=now,
        )

    run_row = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert run_row.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert run_row.provider_run_id is None


def test_ambiguous_submission_timestamp_is_immutable_across_recovery():
    now = timezone.now()
    account = _account('submission-timestamp')
    run = create_or_supersede_feed_run(account.pk, now=now)
    first_claim = claim_due_run_for_account(account.pk, now=now)
    first = mark_feed_submission_unknown(
        first_claim,
        submitted_at=now,
        next_attempt_at=now + timedelta(seconds=1),
        error='lost response',
        now=now,
    )
    replay_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=first.revision,
        now=now + timedelta(seconds=1),
    )
    replay = mark_feed_submission_unknown(
        replay_claim,
        submitted_at=now,
        next_attempt_at=now + timedelta(seconds=2),
        error='still unknown',
        now=now + timedelta(seconds=1),
    )
    conflicting_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=replay.revision,
        now=now + timedelta(seconds=2),
    )

    with pytest.raises(FeedRunConflict, match='timestamp does not match'):
        mark_feed_submission_unknown(
            conflicting_claim,
            submitted_at=now + timedelta(milliseconds=1),
            next_attempt_at=now + timedelta(seconds=3),
            error='different POST',
            now=now + timedelta(seconds=2),
        )

    run_row = MarketplaceFeedRun.objects.get(pk=run.pk)
    assert run_row.submitted_at == now


def test_submission_reconcile_attempt_is_state_scoped_and_resets_when_bound():
    now = timezone.now()
    account = _account('submission-attempt-state')
    _listing(account, 'pending')
    run = create_or_supersede_feed_run(account.pk, now=now)
    first_claim = claim_due_run_for_account(account.pk, now=now)
    unknown = mark_feed_submission_unknown(
        first_claim,
        submitted_at=now,
        next_attempt_at=now + timedelta(seconds=1),
        error='lost response',
        now=now,
    )
    unknown_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=unknown.revision,
        now=now + timedelta(seconds=1),
    )
    retried = retry_step(
        unknown_claim,
        next_attempt_at=now + timedelta(seconds=2),
        error='authoritative negative',
        increment_submission_attempt=True,
        now=now + timedelta(seconds=1),
    )

    assert retried.submission_reconcile_attempt == 1
    rebound_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=retried.revision,
        now=now + timedelta(seconds=2),
    )
    observed = record_provider_run_observation(
        rebound_claim,
        provider_run_id='upload-reset-attempt',
        next_attempt_at=now + timedelta(seconds=3),
        now=now + timedelta(seconds=2),
    )
    assert observed.submission_reconcile_attempt == 0

    polling_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=observed.revision,
        now=now + timedelta(seconds=3),
    )
    with pytest.raises(FeedRunConflict, match='ambiguous submission'):
        retry_step(
            polling_claim,
            next_attempt_at=now + timedelta(seconds=4),
            error='not a submission reconciliation',
            increment_submission_attempt=True,
            now=now + timedelta(seconds=3),
        )


def test_mark_submitted_resets_prior_submission_reconcile_attempt():
    now = timezone.now()
    account = _account('submission-attempt-submit-reset')
    run = create_or_supersede_feed_run(account.pk, now=now)
    first_claim = claim_due_run_for_account(account.pk, now=now)
    unknown = mark_feed_submission_unknown(
        first_claim,
        submitted_at=now,
        next_attempt_at=now + timedelta(seconds=1),
        error='lost response',
        now=now,
    )
    unknown_claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=unknown.revision,
        now=now + timedelta(seconds=1),
    )
    retried = retry_step(
        unknown_claim,
        next_attempt_at=now + timedelta(seconds=2),
        error='authoritative negative',
        increment_submission_attempt=True,
        now=now + timedelta(seconds=1),
    )
    resubmit_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=retried.revision,
        now=now + timedelta(seconds=2),
    )

    submitted = mark_feed_submitted(
        resubmit_claim,
        payload_sha256='c' * 64,
        provider_run_id=None,
        submitted_at=now,
        next_attempt_at=now + timedelta(seconds=3),
        now=now + timedelta(seconds=2),
    )

    assert submitted.state == MarketplaceFeedRun.State.POLLING
    assert submitted.submission_reconcile_attempt == 0


def test_apply_poll_page_commits_listing_logs_counters_cursor_and_account_due():
    now = timezone.now()
    occurred_at = now + timedelta(seconds=1)
    account = _account('apply-poll')
    published = _listing(account, 'published')
    unresolved = _listing(account, 'unresolved')
    published.remote_status = Listing.REMOTE_STATUS_REJECTED
    published.remote_status_checked_at = now
    published.status_check_claim_token = uuid4()
    published.status_check_claimed_until = now + timedelta(minutes=1)
    published.save(update_fields=[
        'remote_status',
        'remote_status_checked_at',
        'status_check_claim_token',
        'status_check_claimed_until',
    ])
    submitted = _submitted_run(account, now)
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=submitted.pk,
        expected_revision=submitted.revision,
        now=now,
    )
    batch = load_poll_batch(claim, now=now)

    result = apply_poll_page(
        claim,
        batch_listing_ids=tuple(listing.pk for listing in batch),
        resolved_external_ids={published.pk: ' remote-123 '},
        last_listing_id=batch[-1].pk,
        next_attempt_at=occurred_at + timedelta(seconds=15),
        occurred_at=occurred_at,
    )

    published.refresh_from_db()
    unresolved.refresh_from_db()
    account.refresh_from_db()
    assert result.changed_listing_ids == (published.pk,)
    assert result.changed_count == 1
    assert result.published_count == 1
    assert result.rejected_count == 0
    assert result.snapshot.poll_cursor_listing_id == unresolved.pk
    assert result.snapshot.published_count == 1
    assert result.snapshot.pending_count == 1
    assert published.status == Listing.STATUS_ACTIVE
    assert published.external_id == 'remote-123'
    assert published.published_at == occurred_at
    assert published.rejection_reason == ''
    assert published.remote_status is None
    assert published.remote_status_checked_at is None
    assert published.status_check_claim_token is None
    assert published.status_check_claimed_until is None
    assert published.next_status_check_at == occurred_at + timedelta(minutes=10)
    assert account.status_batch_due_at == published.next_status_check_at
    assert unresolved.status == Listing.STATUS_PENDING
    assert unresolved.external_id is None
    assert SyncLog.objects.filter(
        listing=published,
        event_type=SyncLog.EVENT_LISTING_PUBLISH,
        status=SyncLog.STATUS_OK,
    ).count() == 1

    with pytest.raises((FeedRunConflict, StaleFeedRunClaim)):
        apply_poll_page(
            claim,
            batch_listing_ids=tuple(listing.pk for listing in batch),
            resolved_external_ids={published.pk: 'remote-123'},
            last_listing_id=batch[-1].pk,
            next_attempt_at=occurred_at + timedelta(seconds=30),
            occurred_at=occurred_at + timedelta(seconds=2),
        )
    assert SyncLog.objects.filter(listing=published).count() == 1


def test_apply_poll_page_rejects_non_exact_or_oversized_batches_without_mutation():
    now = timezone.now()
    account = _account('poll-bounds')
    first = _listing(account, 'first')
    second = _listing(account, 'second')
    submitted = _submitted_run(account, now)
    claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)

    with pytest.raises(ValueError, match='between 1 and 100'):
        apply_poll_page(
            claim,
            batch_listing_ids=tuple(range(1, 102)),
            resolved_external_ids={},
            last_listing_id=101,
            next_attempt_at=now,
            occurred_at=now,
        )
    with pytest.raises(FeedRunConflict, match='exact poll batch'):
        apply_poll_page(
            claim,
            batch_listing_ids=(first.pk,),
            resolved_external_ids={first.pk: 'remote-first'},
            last_listing_id=first.pk,
            next_attempt_at=now,
            occurred_at=now,
        )
    with pytest.raises(ValueError, match='1 to 100'):
        apply_poll_page(
            claim,
            batch_listing_ids=(first.pk, second.pk),
            resolved_external_ids={first.pk: 'x' * 101},
            last_listing_id=second.pk,
            next_attempt_at=now,
            occurred_at=now,
        )

    first.refresh_from_db()
    run = MarketplaceFeedRun.objects.get(pk=submitted.pk)
    assert first.status == Listing.STATUS_PENDING
    assert first.external_id is None
    assert run.revision == claim.revision
    assert run.claim_token == claim.claim_token
    assert run.poll_cursor_listing_id == 0
    assert not SyncLog.objects.filter(listing=first).exists()


def test_stale_generation_cannot_apply_poll_page():
    now = timezone.now()
    account = _account('poll-stale-generation')
    listing = _listing(account, 'listing')
    submitted = _submitted_run(account, now)
    claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)
    # A newer local publish intent may reset membership while the provider call
    # is in flight.  The exact page check must fence that stale HTTP result.
    Listing.objects.filter(pk=listing.pk).update(feed_run_id=None)

    with pytest.raises((FeedRunConflict, StaleFeedRunClaim)):
        apply_poll_page(
            claim,
            batch_listing_ids=(listing.pk,),
            resolved_external_ids={listing.pk: 'stale-remote-id'},
            last_listing_id=listing.pk,
            next_attempt_at=now + timedelta(seconds=3),
            occurred_at=now + timedelta(seconds=2),
        )

    listing.refresh_from_db()
    assert listing.feed_run_id is None
    assert listing.status == Listing.STATUS_PENDING
    assert listing.external_id is None
    assert not SyncLog.objects.filter(listing=listing).exists()


def test_apply_report_page_is_sanitized_idempotent_and_recomputes_terminal_counters():
    now = timezone.now()
    account = _account('apply-report')
    first = _listing(account, 'first')
    second = _listing(account, 'second')
    submitted = _submitted_run(account, now)
    poll_claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)
    reporting = start_reporting(
        poll_claim,
        provider_run_id='upload-1',
        next_attempt_at=now,
        now=now,
    )
    report_claim = claim_due_run_for_account(account.pk, expected_revision=reporting.revision, now=now)
    unsafe_reason = '<b>Invalid</b>\n\x00 field ' + ('x' * 3000)

    first_page = apply_report_page(
        report_claim,
        current_page=1,
        errors_by_ad_id={first.publish_idempotency_key: unsafe_reason},
        next_page=2,
        next_attempt_at=now + timedelta(seconds=15),
        occurred_at=now,
    )

    first.refresh_from_db()
    assert first_page.changed_listing_ids == (first.pk,)
    assert first_page.rejected_count == 1
    assert first_page.snapshot.report_page == 2
    assert first.status == Listing.STATUS_REJECTED
    assert first.rejection_reason.startswith('Invalid field ')
    assert len(first.rejection_reason) == 2000
    assert SyncLog.objects.filter(listing=first).count() == 1
    with pytest.raises(StaleFeedRunClaim):
        apply_report_page(
            report_claim,
            current_page=1,
            errors_by_ad_id={first.publish_idempotency_key: unsafe_reason},
            next_page=2,
            next_attempt_at=now + timedelta(seconds=20),
            occurred_at=now + timedelta(seconds=1),
        )
    assert SyncLog.objects.filter(listing=first).count() == 1

    terminal_claim = claim_due_run_for_account(
        account.pk,
        expected_revision=first_page.snapshot.revision,
        now=now + timedelta(seconds=15),
    )
    MarketplaceFeedRun.objects.filter(pk=submitted.pk).update(
        published_count=8,
        rejected_count=0,
        pending_count=999,
    )
    terminal = apply_report_page(
        terminal_claim,
        current_page=2,
        errors_by_ad_id={second.publish_idempotency_key: 'No category'},
        next_page=None,
        next_attempt_at=None,
        occurred_at=now + timedelta(seconds=15),
    )

    assert terminal.snapshot.state == MarketplaceFeedRun.State.SUCCEEDED
    assert terminal.snapshot.total_count == 2
    assert terminal.snapshot.published_count == 0
    assert terminal.snapshot.rejected_count == 2
    assert terminal.snapshot.pending_count == 0
    assert terminal.snapshot.report_completed_at == now + timedelta(seconds=15)
    assert terminal.snapshot.other_resolved_count == 0
    assert terminal.snapshot.finished_at == now + timedelta(seconds=15)
    assert SyncLog.objects.filter(
        listing_id__in=(first.pk, second.pk),
    ).count() == 2


def test_final_report_with_unresolved_rows_returns_to_bounded_polling():
    now = timezone.now()
    account = _account('report-unresolved')
    rejected = _listing(account, 'rejected')
    unresolved = _listing(account, 'unresolved')
    submitted = _submitted_run(account, now)
    poll_claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)
    reporting = start_reporting(
        poll_claim,
        provider_run_id='upload-1',
        next_attempt_at=now,
        now=now,
    )
    report_claim = claim_due_run_for_account(account.pk, expected_revision=reporting.revision, now=now)

    result = apply_report_page(
        report_claim,
        current_page=1,
        errors_by_ad_id={rejected.publish_idempotency_key: 'Rejected'},
        next_page=None,
        next_attempt_at=None,
        occurred_at=now,
    )

    rejected.refresh_from_db()
    unresolved.refresh_from_db()
    assert rejected.status == Listing.STATUS_REJECTED
    assert unresolved.status == Listing.STATUS_PENDING
    assert result.snapshot.state == MarketplaceFeedRun.State.POLLING
    assert result.snapshot.finished_at is None
    assert result.snapshot.poll_cursor_listing_id == 0
    assert result.snapshot.poll_round == 1
    assert result.snapshot.report_page == 1
    assert result.snapshot.report_completed_at == now
    assert result.snapshot.rejected_count == 1
    assert result.snapshot.pending_count == 1
    assert result.snapshot.next_attempt_at == now + timedelta(minutes=30)


def test_report_pagination_cannot_advance_beyond_100_pages():
    now = timezone.now()
    account = _account('report-page-limit')
    _listing(account, 'pending')
    submitted = _submitted_run(account, now)
    poll_claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)
    reporting = start_reporting(
        poll_claim,
        provider_run_id='upload-1',
        next_attempt_at=now,
        now=now,
    )
    MarketplaceFeedRun.objects.filter(pk=reporting.pk).update(report_page=100)
    page_100_claim = claim_due_run_for_account(account.pk, now=now)

    with pytest.raises(ValueError, match='between 1 and 99'):
        advance_report_page(
            page_100_claim,
            current_page=100,
            next_attempt_at=now,
            now=now,
        )
    with pytest.raises(ValueError, match='must not exceed 100'):
        apply_report_page(
            page_100_claim,
            current_page=100,
            errors_by_ad_id={},
            next_page=101,
            next_attempt_at=now,
            occurred_at=now,
        )


def test_apply_poll_page_rolls_back_rows_logs_and_cursor_if_audit_write_fails(monkeypatch):
    now = timezone.now()
    account = _account('poll-rollback')
    listing = _listing(account, 'listing')
    submitted = _submitted_run(account, now)
    claim = claim_due_run_for_account(account.pk, expected_revision=submitted.revision, now=now)

    def fail_bulk_create(_manager, *_args, **_kwargs):
        raise RuntimeError('audit database unavailable')

    monkeypatch.setattr(type(SyncLog.objects), 'bulk_create', fail_bulk_create)
    with pytest.raises(RuntimeError, match='audit database unavailable'):
        apply_poll_page(
            claim,
            batch_listing_ids=(listing.pk,),
            resolved_external_ids={listing.pk: 'remote-listing'},
            last_listing_id=listing.pk,
            next_attempt_at=now + timedelta(seconds=15),
            occurred_at=now,
        )

    listing.refresh_from_db()
    account.refresh_from_db()
    run = MarketplaceFeedRun.objects.get(pk=submitted.pk)
    assert listing.status == Listing.STATUS_PENDING
    assert listing.external_id is None
    assert listing.next_status_check_at is None
    assert account.status_batch_due_at is None
    assert run.revision == claim.revision
    assert run.claim_token == claim.claim_token
    assert run.poll_cursor_listing_id == 0
    assert run.published_count == 0
    assert run.pending_count == 1
