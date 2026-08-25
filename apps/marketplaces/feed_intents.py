"""Transactional source-intent primitives for marketplace feeds.

These helpers deliberately do not open their own transaction.  A feed intent
is useful only when it commits (or rolls back) with the domain mutation that
changed the feed projection, so every caller must already be inside its
application ``transaction.atomic()`` boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint


# PostgreSQL stores PositiveBigIntegerField in a signed bigint even though the
# Django field rejects negative values.  Keep the boundary explicit so an
# overflow never degrades into a database-specific error after another account
# has already been changed.
MAX_FEED_INTENT_REVISION = (1 << 63) - 1
FEED_INTENT_COALESCE_WINDOW = timedelta(hours=1)


class FeedIntentRevisionDriftError(RuntimeError):
    """Account and its stable endpoint disagree on desired feed state."""


def _require_outer_transaction() -> None:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'Marketplace feed intent must be recorded inside the domain '
            'database transaction.',
        )


def _aware_datetime(value: datetime, *, parameter: str) -> datetime:
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError(f'{parameter} must be a timezone-aware datetime.')
    return value


def _account_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError('account_ids must contain positive integer IDs.')
    try:
        normalized = int(str(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            'account_ids must contain positive integer IDs.',
        ) from exc
    if normalized <= 0 or str(normalized) != str(value):
        raise ValueError('account_ids must contain positive integer IDs.')
    return normalized


def _normalized_account_ids(account_ids: Iterable[int]) -> tuple[int, ...]:
    try:
        return tuple(sorted({_account_id(value) for value in account_ids}))
    except TypeError as exc:
        raise ValueError('account_ids must be an iterable of account IDs.') from exc


def _normalized_product_ids(product_ids: Iterable[int]) -> tuple[int, ...]:
    """Normalize product IDs without importing the products app at module load."""

    try:
        return tuple(sorted({_account_id(value) for value in product_ids}))
    except TypeError as exc:
        raise ValueError('product_ids must be an iterable of product IDs.') from exc


def product_feed_account_ids(product_ids: Iterable[int]) -> tuple[int, ...]:
    """Return nondeleted ACTIVE/PENDING owners for the selected products.

    This is a read-only projection probe.  Writers intentionally call it
    before their transaction, then re-run it after taking the account locks so
    a concurrent listing move cannot silently redirect a product mutation to
    an unrecorded account. Paused accounts and tenants remain owners here: a
    product/listing tombstone created during the pause must leave a successor
    intent that becomes scanner-visible on reactivation. Deleted accounts are
    intentionally excluded because their provider ownership is reconciled by
    the account lifecycle workflow.
    """

    from apps.marketplaces.models import Listing

    normalized_ids = _normalized_product_ids(product_ids)
    if not normalized_ids:
        return ()
    return tuple(
        Listing.objects.filter(
            product_id__in=normalized_ids,
            status__in=(Listing.STATUS_ACTIVE, Listing.STATUS_PENDING),
            account__deleted_at__isnull=True,
        )
        .order_by('account_id')
        .values_list('account_id', flat=True)
        .distinct()
    )


def bump_feed_intents_for_products(
    product_ids: Iterable[int],
    observed_at: datetime,
) -> dict[int, int]:
    """Advance each nondeleted product feed owner once in the caller transaction.

    ``legacy`` remains completely inert.  ``dual_write`` records the durable
    cursor while legacy delivery continues, and ``durable`` uses the same
    primitive once that ingress mode is activated.
    """

    if settings.MARKETPLACE_FEED_INGRESS_MODE == 'legacy':
        return {}
    account_ids = product_feed_account_ids(product_ids)
    return bump_feed_intents(account_ids, observed_at)


def _lock_accounts(account_ids: tuple[int, ...]) -> list[MarketplaceAccount]:
    if not account_ids:
        return []
    accounts = list(
        MarketplaceAccount.all_objects.select_for_update()
        .only(
            'id',
            'last_feed_flush_at',
            'feed_intent_revision',
            'feed_intent_dispatched_revision',
            'feed_intent_due_at',
        )
        .filter(pk__in=account_ids)
        .order_by('pk')
    )
    found_ids = tuple(account.pk for account in accounts)
    if found_ids != account_ids:
        missing = sorted(set(account_ids) - set(found_ids))
        raise MarketplaceAccount.DoesNotExist(
            f'Marketplace accounts do not exist: {missing}.',
        )
    return accounts


def _assert_revision_can_advance(account: MarketplaceAccount) -> None:
    if account.feed_intent_revision >= MAX_FEED_INTENT_REVISION:
        raise OverflowError(
            f'Marketplace feed intent revision exhausted for account '
            f'{account.pk}.',
        )


def _lock_synchronized_endpoints(
    accounts: list[MarketplaceAccount],
) -> dict[int, MarketplaceFeedEndpoint]:
    """Lock optional stable endpoints after all account locks are held."""

    account_by_id = {account.pk: account for account in accounts}
    if not account_by_id:
        return {}
    endpoints = list(
        MarketplaceFeedEndpoint.objects.select_for_update()
        .only('public_id', 'account_id', 'source_intent_revision')
        .filter(account_id__in=account_by_id)
        .order_by('account_id')
    )
    for endpoint in endpoints:
        account = account_by_id[endpoint.account_id]
        if endpoint.source_intent_revision != account.feed_intent_revision:
            raise FeedIntentRevisionDriftError(
                'Marketplace account and feed endpoint intent revisions '
                f'disagree for account {account.pk}.',
            )
    return {endpoint.account_id: endpoint for endpoint in endpoints}


def _write_accounts(
    accounts: list[MarketplaceAccount],
    *,
    fields: tuple[str, ...],
) -> None:
    if accounts:
        # bulk_update intentionally bypasses TimestampedModel.updated_at.  The
        # generic timestamp describes account configuration changes, not the
        # high-frequency provider-neutral feed scheduler cursor.
        MarketplaceAccount.all_objects.bulk_update(accounts, fields)


def _write_endpoints(endpoints: list[MarketplaceFeedEndpoint]) -> None:
    if endpoints:
        # Keep TimestampedModel.updated_at reserved for endpoint profile and
        # capability lifecycle mutations, not source-intent cursor advances.
        MarketplaceFeedEndpoint.objects.bulk_update(
            endpoints,
            ('source_intent_revision',),
        )


def bump_feed_intents(
    account_ids: Iterable[int],
    observed_at: datetime,
) -> dict[int, int]:
    """Atomically advance each distinct account's desired feed revision.

    Accounts are locked in primary-key order.  The next due time respects the
    one-hour provider coalescing window and never postpones work that is
    already scheduled earlier.
    """

    _require_outer_transaction()
    observed_at = _aware_datetime(observed_at, parameter='observed_at')
    normalized_ids = _normalized_account_ids(account_ids)
    accounts = _lock_accounts(normalized_ids)
    endpoints_by_account = _lock_synchronized_endpoints(accounts)

    # Validate the complete locked set before the first write.  This keeps an
    # overflow or missing-row failure atomic even if a caller catches the
    # exception inside its outer transaction.
    for account in accounts:
        _assert_revision_can_advance(account)

    for account in accounts:
        candidate_due_at = observed_at
        if account.last_feed_flush_at is not None:
            candidate_due_at = max(
                candidate_due_at,
                account.last_feed_flush_at + FEED_INTENT_COALESCE_WINDOW,
            )
        held_undispatched_intent = (
            account.feed_intent_revision
            > account.feed_intent_dispatched_revision
            and account.feed_intent_due_at is None
        )
        if not held_undispatched_intent:
            if account.feed_intent_due_at is None:
                account.feed_intent_due_at = candidate_due_at
            else:
                account.feed_intent_due_at = min(
                    account.feed_intent_due_at,
                    candidate_due_at,
                )
        else:
            # ``revision > dispatched`` with no due cursor is the durable
            # OUTCOME_UNCERTAIN hold.  A newer domain mutation advances the
            # desired state but must not silently release provider ownership.
            account.feed_intent_due_at = None
        account.feed_intent_revision += 1
        endpoint = endpoints_by_account.get(account.pk)
        if endpoint is not None:
            endpoint.source_intent_revision = account.feed_intent_revision

    _write_accounts(
        accounts,
        fields=('feed_intent_revision', 'feed_intent_due_at'),
    )
    _write_endpoints(list(endpoints_by_account.values()))
    return {
        account.pk: account.feed_intent_revision
        for account in accounts
    }


def rearm_feed_intent(
    account_id: int,
    expected_revision: int,
    due_at: datetime | None,
) -> int | None:
    """CAS-create a successor intent after an already-dispatched revision.

    ``due_at=None`` is intentional: it records an undispatched successor held
    by an outcome-uncertain provider generation.  A stale expected revision is
    a benign CAS miss and returns ``None``.
    """

    _require_outer_transaction()
    normalized_id = _account_id(account_id)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise ValueError('expected_revision must be a non-negative integer.')
    if expected_revision < 0 or expected_revision > MAX_FEED_INTENT_REVISION:
        raise ValueError('expected_revision must be a non-negative bigint.')
    if due_at is not None:
        due_at = _aware_datetime(due_at, parameter='due_at')

    account = _lock_accounts((normalized_id,))[0]
    endpoints_by_account = _lock_synchronized_endpoints([account])
    if (
        account.feed_intent_revision != expected_revision
        or account.feed_intent_dispatched_revision != expected_revision
    ):
        return None
    _assert_revision_can_advance(account)

    account.feed_intent_revision += 1
    account.feed_intent_due_at = due_at
    endpoint = endpoints_by_account.get(account.pk)
    if endpoint is not None:
        endpoint.source_intent_revision = account.feed_intent_revision
    _write_accounts(
        [account],
        fields=('feed_intent_revision', 'feed_intent_due_at'),
    )
    if endpoint is not None:
        _write_endpoints([endpoint])
    return account.feed_intent_revision


def nudge_undispatched_feed_intent(
    account_id: int,
    due_at: datetime,
) -> bool:
    """Make existing undispatched work eligible sooner, never later."""

    _require_outer_transaction()
    normalized_id = _account_id(account_id)
    due_at = _aware_datetime(due_at, parameter='due_at')
    account = _lock_accounts((normalized_id,))[0]

    if account.feed_intent_revision <= account.feed_intent_dispatched_revision:
        return False
    if account.feed_intent_due_at is not None and account.feed_intent_due_at <= due_at:
        return False

    account.feed_intent_due_at = due_at
    _write_accounts([account], fields=('feed_intent_due_at',))
    return True
