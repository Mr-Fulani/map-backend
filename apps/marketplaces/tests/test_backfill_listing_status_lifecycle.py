import datetime
import json
import uuid
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from apps.marketplaces.management.commands.backfill_listing_status_lifecycle import (
    _due_at,
)
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.models import Tenant


ANCHOR_TEXT = '2026-08-13T00:00:00Z'
ANCHOR = datetime.datetime(2026, 8, 13, tzinfo=datetime.timezone.utc)
_DEFAULT_EXTERNAL_ID = object()


def _tenant(suffix: str) -> Tenant:
    return Tenant.objects.create(name=f'Backfill {suffix}', slug=f'backfill-{suffix}')


def _account(tenant: Tenant, suffix: str, *, active: bool = True) -> MarketplaceAccount:
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name=f'Account {suffix}',
        external_id=f'account-{suffix}',
        is_active=active,
        credentials_enc=b'opaque-test-credentials',
    )


def _listing(
    tenant: Tenant,
    account: MarketplaceAccount,
    suffix: str,
    *,
    status: str = Listing.STATUS_ACTIVE,
    external_id: str | None | object = _DEFAULT_EXTERNAL_ID,
    next_due: datetime.datetime | None = None,
) -> Listing:
    product = Product.objects.create(
        tenant=tenant,
        article=f'BF-{suffix}',
        name=f'Backfill product {suffix}',
        price=Decimal('1000.00'),
    )
    resolved_external_id: str | None
    if external_id is _DEFAULT_EXTERNAL_ID:
        resolved_external_id = f'listing-{suffix}'
    elif external_id is None or isinstance(external_id, str):
        resolved_external_id = external_id
    else:
        raise TypeError('external_id must be a string or None')
    return Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        external_id=resolved_external_id,
        status=status,
        price_on_listing=Decimal('1100.00'),
        next_status_check_at=next_due,
    )


def _run(*args, **kwargs) -> dict:
    output = StringIO()
    call_command(
        'backfill_listing_status_lifecycle',
        *args,
        anchor=ANCHOR_TEXT,
        stdout=output,
        **kwargs,
    )
    return json.loads(output.getvalue())


@pytest.mark.django_db
def test_mutating_backfill_requires_explicit_dual_write_mode():
    with override_settings(AVITO_STATUS_LIFECYCLE_MODE='legacy'):
        with pytest.raises(CommandError, match='dual_write'):
            _run()


@pytest.mark.django_db
def test_dry_run_is_allowed_in_legacy_mode_for_preflight():
    tenant = _tenant('legacy-dry-run')
    account = _account(tenant, 'legacy-dry-run')
    listing = _listing(tenant, account, 'legacy-dry-run')

    with override_settings(AVITO_STATUS_LIFECYCLE_MODE='legacy'):
        summary = _run(dry_run=True)

    listing.refresh_from_db()
    account.refresh_from_db()
    assert summary['would_update'] == 1
    assert summary['updated'] == 0
    assert listing.next_status_check_at is None
    assert account.status_batch_due_at is None


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
@pytest.mark.parametrize(
    'anchor',
    [
        '2026-08-13T00:00:00',
        '2026-08-13T03:00:00+03:00',
        'not-a-timestamp',
    ],
)
def test_backfill_rejects_non_utc_or_naive_anchor(anchor):
    with pytest.raises(CommandError, match='aware UTC'):
        call_command('backfill_listing_status_lifecycle', anchor=anchor)


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_dry_run_is_bounded_structured_and_does_not_mutate():
    tenant = _tenant('dry-run')
    account = _account(tenant, 'dry-run')
    first = _listing(tenant, account, 'dry-run-1', status=Listing.STATUS_ACTIVE)
    second = _listing(tenant, account, 'dry-run-2', status=Listing.STATUS_PENDING)

    summary = _run(dry_run=True, max_rows=1, batch_size=1)

    first.refresh_from_db()
    second.refresh_from_db()
    account.refresh_from_db()
    assert first.next_status_check_at is None
    assert second.next_status_check_at is None
    assert account.status_batch_due_at is None
    assert summary['mode'] == 'dry_run'
    assert summary['anchor'] == ANCHOR_TEXT
    assert summary['batch_size'] == 1
    assert summary['max_rows'] == 1
    assert summary['eligible_before'] == 2
    assert summary['candidates'] == 1
    assert summary['considered'] == 1
    assert summary['would_update'] == 1
    assert summary['updated'] == 0
    assert summary['accounts_would_update'] == 1
    assert summary['accounts_updated'] == 0
    assert summary['eligible_after'] == 2
    assert summary['last_pk'] == first.pk
    assert summary['duration_seconds'] >= 0


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_backfill_updates_only_eligible_rows_and_preserves_observations_and_claims():
    tenant = _tenant('eligibility')
    account = _account(tenant, 'eligibility')
    cooldown = ANCHOR + datetime.timedelta(hours=2)
    account.status_batch_cooldown_until = cooldown
    account.status_batch_claim_token = uuid.uuid4()
    account.status_batch_claimed_until = ANCHOR + datetime.timedelta(minutes=30)
    account.save(update_fields=[
        'status_batch_cooldown_until',
        'status_batch_claim_token',
        'status_batch_claimed_until',
    ])

    active = _listing(tenant, account, 'eligible-active', status=Listing.STATUS_ACTIVE)
    pending = _listing(tenant, account, 'eligible-pending', status=Listing.STATUS_PENDING)
    archiving = _listing(
        tenant,
        account,
        'eligible-archiving',
        status=Listing.STATUS_ARCHIVING,
    )
    active.remote_status = Listing.REMOTE_STATUS_ACTIVE
    active.remote_status_checked_at = ANCHOR - datetime.timedelta(hours=1)
    active.status_check_claim_token = uuid.uuid4()
    active.status_check_claimed_until = ANCHOR + datetime.timedelta(minutes=5)
    active.save(update_fields=[
        'remote_status',
        'remote_status_checked_at',
        'status_check_claim_token',
        'status_check_claimed_until',
    ])
    observation = (
        active.remote_status,
        active.remote_status_checked_at,
        active.status_check_claim_token,
        active.status_check_claimed_until,
    )

    draft = _listing(tenant, account, 'draft', status=Listing.STATUS_DRAFT)
    empty_external = _listing(
        tenant,
        account,
        'empty-external',
        status=Listing.STATUS_ACTIVE,
        external_id='',
    )
    null_external = _listing(
        tenant,
        account,
        'null-external',
        status=Listing.STATUS_ACTIVE,
        external_id=None,
    )
    existing_due = ANCHOR - datetime.timedelta(minutes=1)
    already_due = _listing(
        tenant,
        account,
        'already-due',
        status=Listing.STATUS_ACTIVE,
        next_due=existing_due,
    )
    inactive_account = _account(tenant, 'inactive', active=False)
    inactive = _listing(
        tenant,
        inactive_account,
        'inactive-account',
        status=Listing.STATUS_ACTIVE,
    )
    inactive_tenant = _tenant('inactive-tenant')
    inactive_tenant.is_active = False
    inactive_tenant.save(update_fields=['is_active'])
    inactive_tenant_account = _account(inactive_tenant, 'inactive-tenant')
    inactive_tenant_listing = _listing(
        inactive_tenant,
        inactive_tenant_account,
        'inactive-tenant',
        status=Listing.STATUS_ACTIVE,
    )
    deleted = _listing(
        tenant,
        account,
        'soft-deleted',
        status=Listing.STATUS_ACTIVE,
    )
    deleted.soft_delete()

    summary = _run(batch_size=2)

    for listing in (
        active,
        pending,
        archiving,
        draft,
        empty_external,
        null_external,
        already_due,
        inactive,
        inactive_tenant_listing,
        deleted,
    ):
        listing.refresh_from_db()
    account.refresh_from_db()
    inactive_account.refresh_from_db()

    assert ANCHOR <= active.next_status_check_at < ANCHOR + datetime.timedelta(hours=24)
    for listing in (pending, archiving):
        assert ANCHOR <= listing.next_status_check_at < ANCHOR + datetime.timedelta(minutes=10)
    assert (
        active.remote_status,
        active.remote_status_checked_at,
        active.status_check_claim_token,
        active.status_check_claimed_until,
    ) == observation
    assert draft.next_status_check_at is None
    assert empty_external.next_status_check_at is None
    assert null_external.next_status_check_at is None
    assert already_due.next_status_check_at == existing_due
    assert inactive.next_status_check_at is None
    assert inactive_tenant_listing.next_status_check_at is None
    assert deleted.next_status_check_at is None
    assert account.status_batch_due_at == min(
        existing_due,
        active.next_status_check_at,
        pending.next_status_check_at,
        archiving.next_status_check_at,
    )
    assert account.status_batch_cooldown_until == cooldown
    assert account.status_batch_claim_token is not None
    assert account.status_batch_claimed_until == ANCHOR + datetime.timedelta(minutes=30)
    assert inactive_account.status_batch_due_at is None
    assert summary['updated'] == 3
    assert summary['would_update'] == 0
    assert summary['eligible_before'] == 3
    assert summary['eligible_after'] == 0
    assert summary['status_counts'] == {
        Listing.STATUS_ACTIVE: 1,
        Listing.STATUS_ARCHIVING: 1,
        Listing.STATUS_PENDING: 1,
    }
    assert summary['claim_mismatches_before'] == {'listing': 0, 'account': 0}
    assert summary['claim_mismatches_after'] == {'listing': 0, 'account': 0}


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_backfill_filters_by_tenant_and_account():
    selected_tenant = _tenant('selected')
    other_tenant = _tenant('other')
    selected_account = _account(selected_tenant, 'selected')
    other_account = _account(selected_tenant, 'other-same-tenant')
    foreign_account = _account(other_tenant, 'foreign')
    selected = _listing(selected_tenant, selected_account, 'selected')
    same_tenant = _listing(selected_tenant, other_account, 'same-tenant')
    foreign = _listing(other_tenant, foreign_account, 'foreign')

    first = _run(tenant_id=selected_tenant.pk, account_id=selected_account.pk)
    assert first['updated'] == 1
    for listing in (selected, same_tenant, foreign):
        listing.refresh_from_db()
    assert selected.next_status_check_at is not None
    assert same_tenant.next_status_check_at is None
    assert foreign.next_status_check_at is None

    second = _run(tenant_id=selected_tenant.pk)
    assert second['updated'] == 1
    same_tenant.refresh_from_db()
    foreign.refresh_from_db()
    assert same_tenant.next_status_check_at is not None
    assert foreign.next_status_check_at is None


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_backfill_max_rows_is_resumable_and_idempotent_for_same_anchor():
    tenant = _tenant('resume')
    account = _account(tenant, 'resume')
    listings = [
        _listing(tenant, account, f'resume-{index}', status=Listing.STATUS_ACTIVE)
        for index in range(3)
    ]

    first = _run(batch_size=1, max_rows=2, account_id=account.pk)
    assert first['updated'] == 2
    assert first['batches'] == 2
    assert first['eligible_after'] == 1
    for listing in listings:
        listing.refresh_from_db()
    original_due = {
        listing.pk: listing.next_status_check_at
        for listing in listings
        if listing.next_status_check_at is not None
    }
    assert len(original_due) == 2

    second = _run(batch_size=1, max_rows=2, account_id=account.pk)
    assert second['updated'] == 1
    assert second['eligible_after'] == 0
    for listing in listings:
        listing.refresh_from_db()
    assert all(listing.next_status_check_at is not None for listing in listings)
    assert {
        listing.pk: listing.next_status_check_at
        for listing in listings
        if listing.pk in original_due
    } == original_due

    third = _run(account_id=account.pk)
    assert third['eligible_before'] == 0
    assert third['updated'] == 0
    assert third['batches'] == 0


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_backfill_repairs_account_cursor_when_listing_due_is_already_present():
    tenant = _tenant('cursor-repair')
    account = _account(tenant, 'cursor-repair')
    first_due = ANCHOR + datetime.timedelta(minutes=15)
    later_due = ANCHOR + datetime.timedelta(hours=2)
    _listing(
        tenant,
        account,
        'cursor-repair-first',
        next_due=first_due,
    )
    _listing(
        tenant,
        account,
        'cursor-repair-later',
        next_due=later_due,
    )
    account.status_batch_due_at = later_due
    account.save(update_fields=['status_batch_due_at'])

    summary = _run(account_id=account.pk)

    account.refresh_from_db()
    assert summary['eligible_before'] == 0
    assert summary['updated'] == 0
    assert summary['account_cursor_mismatches_before'] == 1
    assert summary['account_cursors_repaired'] == 1
    assert summary['account_cursor_mismatches_after'] == 0
    assert account.status_batch_due_at == first_due

    rerun = _run(account_id=account.pk)
    assert rerun['account_cursor_mismatches_before'] == 0
    assert rerun['account_cursors_repaired'] == 0


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_cursor_repair_recomputes_minimum_after_account_lock(monkeypatch):
    tenant = _tenant('cursor-race')
    account = _account(tenant, 'cursor-race')
    stale_due = ANCHOR + datetime.timedelta(hours=3)
    newer_due = ANCHOR + datetime.timedelta(minutes=2)
    listing = _listing(
        tenant,
        account,
        'cursor-race',
        next_due=stale_due,
    )
    original_due_minima = (
        __import__(
            'apps.marketplaces.management.commands.'
            'backfill_listing_status_lifecycle',
            fromlist=['Command'],
        ).Command._due_minima
    )
    calls = {'count': 0}

    def concurrent_minimum(self, **kwargs):
        calls['count'] += 1
        if calls['count'] == 1:
            Listing.objects.filter(pk=listing.pk).update(
                next_status_check_at=newer_due,
            )
            MarketplaceAccount.objects.filter(pk=account.pk).update(
                status_batch_due_at=newer_due,
            )
        return original_due_minima(self, **kwargs)

    monkeypatch.setattr(
        'apps.marketplaces.management.commands.'
        'backfill_listing_status_lifecycle.Command._due_minima',
        concurrent_minimum,
    )

    summary = _run(account_id=account.pk)

    account.refresh_from_db()
    assert calls['count'] >= 1
    assert account.status_batch_due_at == newer_due
    assert summary['account_cursor_mismatches_after'] == 0


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_max_rows_bounds_attempts_when_every_candidate_is_lock_skipped(monkeypatch):
    tenant = _tenant('locked-bound')
    account = _account(tenant, 'locked-bound')
    for index in range(5):
        _listing(tenant, account, f'locked-bound-{index}')

    def skip_entire_batch(self, **kwargs):
        return {
            'considered': 0,
            'updated': 0,
            'account_ids_updated': set(),
            'status_counts': {},
        }

    monkeypatch.setattr(
        'apps.marketplaces.management.commands.'
        'backfill_listing_status_lifecycle.Command._apply_batch',
        skip_entire_batch,
    )

    summary = _run(account_id=account.pk, batch_size=2, max_rows=2)

    assert summary['candidates'] == 2
    assert summary['considered'] == 0
    assert summary['skipped_concurrent'] == 2
    assert summary['batches'] == 1
    assert summary['eligible_after'] == 5


def test_due_jitter_is_deterministic_and_bounded():
    active_first = _due_at(
        anchor=ANCHOR,
        listing_id=123,
        status=Listing.STATUS_ACTIVE,
    )
    active_second = _due_at(
        anchor=ANCHOR,
        listing_id=123,
        status=Listing.STATUS_ACTIVE,
    )
    transient = _due_at(
        anchor=ANCHOR,
        listing_id=123,
        status=Listing.STATUS_PENDING,
    )
    assert active_first == active_second
    assert ANCHOR <= active_first < ANCHOR + datetime.timedelta(hours=24)
    assert ANCHOR <= transient < ANCHOR + datetime.timedelta(minutes=10)


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_apply_sets_local_postgresql_deadlines_inside_atomic():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL transaction-local timeout contract')
    tenant = _tenant('timeouts')
    account = _account(tenant, 'timeouts')
    _listing(tenant, account, 'timeouts')
    statements = []

    def capture(execute, sql, params, many, context):
        statements.append(' '.join(sql.lower().split()))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        summary = _run(account_id=account.pk)

    assert summary['updated'] == 1
    assert "set local lock_timeout to '1s'" in statements
    assert "set local statement_timeout to '15s'" in statements


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
@pytest.mark.parametrize(
    ('option', 'value', 'message'),
    [
        ('batch_size', 0, '--batch-size'),
        ('batch_size', 1_001, '1000'),
        ('max_rows', 0, '--max-rows'),
        ('tenant_id', 0, '--tenant-id'),
        ('account_id', -1, '--account-id'),
    ],
)
def test_backfill_rejects_invalid_bounds(option, value, message):
    with pytest.raises(CommandError, match=message):
        _run(**{option: value})
