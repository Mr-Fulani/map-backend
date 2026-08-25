from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.marketplaces.feed_intents import (
    FeedIntentRevisionDriftError,
    MAX_FEED_INTENT_REVISION,
    bump_feed_intents,
    nudge_undispatched_feed_intent,
    rearm_feed_intent,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.tenants.models import Tenant


def _account(*, slug: str, **values) -> MarketplaceAccount:
    tenant = Tenant.objects.create(name=f'Feed intent {slug}', slug=slug)
    defaults = {
        'tenant': tenant,
        'marketplace': MarketplaceAccount.MARKETPLACE_AVITO,
        'name': f'Feed intent account {slug}',
        'external_id': f'{slug}-external',
        'credentials_enc': b'opaque-test-credentials',
    }
    defaults.update(values)
    return MarketplaceAccount.objects.create(**defaults)


def _endpoint(
    account: MarketplaceAccount,
    *,
    source_intent_revision: int | None = None,
) -> MarketplaceFeedEndpoint:
    if source_intent_revision is None:
        source_intent_revision = account.feed_intent_revision
    return MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=account_identity_digest(account),
        source_intent_revision=source_intent_revision,
    )


@pytest.mark.django_db(transaction=True)
def test_feed_intent_primitives_require_existing_outer_transaction():
    account = _account(slug='requires-outer-transaction')
    due_at = timezone.now()
    assert connection.in_atomic_block is False

    with pytest.raises(RuntimeError, match='inside the domain database transaction'):
        bump_feed_intents([account.pk], due_at)
    with pytest.raises(RuntimeError, match='inside the domain database transaction'):
        rearm_feed_intent(account.pk, 0, due_at)
    with pytest.raises(RuntimeError, match='inside the domain database transaction'):
        nudge_undispatched_feed_intent(account.pk, due_at)

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
def test_bump_feed_intents_deduplicates_and_locks_accounts_in_pk_order():
    first = _account(slug='dedupe-first')
    second = _account(slug='dedupe-second')
    observed_at = timezone.now()

    with transaction.atomic():
        revisions = bump_feed_intents(
            [second.pk, first.pk, second.pk, first.pk],
            observed_at,
        )

    assert list(revisions) == [first.pk, second.pk]
    assert revisions == {first.pk: 1, second.pk: 1}
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.feed_intent_revision == 1
    assert second.feed_intent_revision == 1
    assert first.feed_intent_due_at == observed_at
    assert second.feed_intent_due_at == observed_at


@pytest.mark.django_db
def test_bump_feed_intents_respects_each_account_coalescing_window():
    observed_at = timezone.now()
    recent = _account(
        slug='recent-flush-window',
        last_feed_flush_at=observed_at - timedelta(minutes=15),
    )
    old = _account(
        slug='old-flush-window',
        last_feed_flush_at=observed_at - timedelta(hours=2),
    )
    future = _account(
        slug='future-flush-window',
        last_feed_flush_at=observed_at + timedelta(minutes=5),
    )

    with transaction.atomic():
        bump_feed_intents([recent.pk, old.pk, future.pk], observed_at)

    recent.refresh_from_db()
    old.refresh_from_db()
    future.refresh_from_db()
    assert recent.feed_intent_due_at == observed_at + timedelta(minutes=45)
    assert old.feed_intent_due_at == observed_at
    assert future.feed_intent_due_at == observed_at + timedelta(minutes=65)


@pytest.mark.django_db
def test_bump_feed_intents_never_postpones_an_earlier_due_intent():
    observed_at = timezone.now()
    earlier_due_at = observed_at - timedelta(minutes=10)
    account = _account(
        slug='preserve-earlier-due',
        last_feed_flush_at=observed_at,
        feed_intent_revision=7,
        feed_intent_dispatched_revision=6,
        feed_intent_due_at=earlier_due_at,
    )

    with transaction.atomic():
        revisions = bump_feed_intents([account.pk], observed_at)

    account.refresh_from_db()
    assert revisions == {account.pk: 8}
    assert account.feed_intent_revision == 8
    assert account.feed_intent_dispatched_revision == 6
    assert account.feed_intent_due_at == earlier_due_at


@pytest.mark.django_db
def test_bump_preserves_existing_outcome_uncertain_hold():
    account = _account(
        slug='preserve-uncertain-hold',
        feed_intent_revision=7,
        feed_intent_dispatched_revision=6,
        feed_intent_due_at=None,
    )

    with transaction.atomic():
        revisions = bump_feed_intents([account.pk], timezone.now())

    account.refresh_from_db()
    assert revisions == {account.pk: 8}
    assert account.feed_intent_revision == 8
    assert account.feed_intent_dispatched_revision == 6
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
def test_bump_feed_intents_rolls_back_with_the_domain_mutation():
    account = _account(slug='domain-rollback')
    endpoint = _endpoint(account)
    original_name = account.name
    observed_at = timezone.now()

    with pytest.raises(ValueError, match='abort domain write'):
        with transaction.atomic():
            account.name = 'must roll back'
            account.save(update_fields=['name'])
            bump_feed_intents([account.pk], observed_at)
            raise ValueError('abort domain write')

    account.refresh_from_db()
    assert account.name == original_name
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None
    endpoint.refresh_from_db()
    assert endpoint.source_intent_revision == 0


@pytest.mark.django_db
def test_bump_and_rearm_keep_existing_endpoint_revision_in_sync():
    account = _account(slug='endpoint-revision-sync')
    endpoint = _endpoint(account)
    due_at = timezone.now()

    with transaction.atomic():
        assert bump_feed_intents([account.pk], due_at) == {account.pk: 1}

    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert endpoint.source_intent_revision == 1

    MarketplaceAccount.all_objects.filter(pk=account.pk).update(
        feed_intent_dispatched_revision=1,
        feed_intent_due_at=None,
    )
    with transaction.atomic():
        assert rearm_feed_intent(account.pk, 1, None) == 2

    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.feed_intent_revision == 2
    assert endpoint.source_intent_revision == 2


@pytest.mark.django_db
def test_endpoint_revision_drift_fails_before_any_account_or_endpoint_write():
    synchronized = _account(slug='endpoint-drift-synchronized')
    synchronized_endpoint = _endpoint(synchronized)
    drifted = _account(slug='endpoint-drift-existing')
    drifted_endpoint = _endpoint(drifted, source_intent_revision=1)

    # Catch inside the caller's transaction: drift validation itself must
    # guarantee zero partial writes rather than relying on exception rollback.
    with transaction.atomic():
        with pytest.raises(FeedIntentRevisionDriftError, match='disagree'):
            bump_feed_intents(
                [synchronized.pk, drifted.pk],
                timezone.now(),
            )

    synchronized.refresh_from_db()
    drifted.refresh_from_db()
    synchronized_endpoint.refresh_from_db()
    drifted_endpoint.refresh_from_db()
    assert synchronized.feed_intent_revision == 0
    assert synchronized.feed_intent_due_at is None
    assert synchronized_endpoint.source_intent_revision == 0
    assert drifted.feed_intent_revision == 0
    assert drifted.feed_intent_due_at is None
    assert drifted_endpoint.source_intent_revision == 1


@pytest.mark.django_db
def test_rearm_fails_closed_on_endpoint_revision_drift():
    account = _account(
        slug='endpoint-drift-rearm',
        feed_intent_revision=3,
        feed_intent_dispatched_revision=3,
    )
    endpoint = _endpoint(account, source_intent_revision=2)

    with transaction.atomic():
        with pytest.raises(FeedIntentRevisionDriftError, match='disagree'):
            rearm_feed_intent(account.pk, 3, timezone.now())

    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.feed_intent_revision == 3
    assert account.feed_intent_due_at is None
    assert endpoint.source_intent_revision == 2


@pytest.mark.django_db
def test_bump_and_rearm_allow_account_without_feed_endpoint():
    account = _account(slug='no-feed-endpoint')
    due_at = timezone.now()

    with transaction.atomic():
        assert bump_feed_intents([account.pk], due_at) == {account.pk: 1}
    MarketplaceAccount.all_objects.filter(pk=account.pk).update(
        feed_intent_dispatched_revision=1,
        feed_intent_due_at=None,
    )
    with transaction.atomic():
        assert rearm_feed_intent(account.pk, 1, None) == 2

    account.refresh_from_db()
    assert account.feed_intent_revision == 2
    assert account.feed_intent_dispatched_revision == 1
    assert account.feed_intent_due_at is None
    assert not MarketplaceFeedEndpoint.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_bump_overflow_fails_before_any_account_write_even_if_caught():
    normal = _account(slug='overflow-normal')
    exhausted = _account(
        slug='overflow-exhausted',
        feed_intent_revision=MAX_FEED_INTENT_REVISION,
        feed_intent_dispatched_revision=MAX_FEED_INTENT_REVISION,
    )
    observed_at = timezone.now()

    # Catch inside the caller transaction deliberately.  The primitive must
    # preflight every locked row rather than relying on exception rollback.
    with transaction.atomic():
        with pytest.raises(OverflowError, match='revision exhausted'):
            bump_feed_intents([normal.pk, exhausted.pk], observed_at)

    normal.refresh_from_db()
    exhausted.refresh_from_db()
    assert normal.feed_intent_revision == 0
    assert normal.feed_intent_due_at is None
    assert exhausted.feed_intent_revision == MAX_FEED_INTENT_REVISION


@pytest.mark.django_db
def test_bump_missing_account_fails_before_any_account_write():
    account = _account(slug='missing-account-atomicity')
    missing_id = account.pk + 100_000

    with transaction.atomic():
        with pytest.raises(MarketplaceAccount.DoesNotExist):
            bump_feed_intents([account.pk, missing_id], timezone.now())

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
def test_rearm_feed_intent_uses_expected_revision_cas_and_exact_due():
    account = _account(
        slug='rearm-cas',
        feed_intent_revision=4,
        feed_intent_dispatched_revision=4,
    )
    due_at = timezone.now() + timedelta(minutes=20)

    with transaction.atomic():
        assert rearm_feed_intent(account.pk, 3, due_at) is None
    account.refresh_from_db()
    assert account.feed_intent_revision == 4
    assert account.feed_intent_due_at is None

    with transaction.atomic():
        assert rearm_feed_intent(account.pk, 4, due_at) == 5
    account.refresh_from_db()
    assert account.feed_intent_revision == 5
    assert account.feed_intent_dispatched_revision == 4
    assert account.feed_intent_due_at == due_at

    # None is an exact, durable hold for an outcome-uncertain predecessor.
    MarketplaceAccount.all_objects.filter(pk=account.pk).update(
        feed_intent_dispatched_revision=5,
        feed_intent_due_at=None,
    )
    with transaction.atomic():
        assert rearm_feed_intent(account.pk, 5, None) == 6
    account.refresh_from_db()
    assert account.feed_intent_revision == 6
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
def test_rearm_does_not_supersede_current_but_undispatched_intent():
    original_due_at = timezone.now() + timedelta(minutes=10)
    account = _account(
        slug='rearm-undispatched-cas-miss',
        feed_intent_revision=4,
        feed_intent_dispatched_revision=3,
        feed_intent_due_at=original_due_at,
    )

    with transaction.atomic():
        result = rearm_feed_intent(
            account.pk,
            4,
            timezone.now() + timedelta(minutes=30),
        )

    assert result is None
    account.refresh_from_db()
    assert account.feed_intent_revision == 4
    assert account.feed_intent_dispatched_revision == 3
    assert account.feed_intent_due_at == original_due_at


@pytest.mark.django_db
def test_rearm_feed_intent_overflow_fails_closed():
    account = _account(
        slug='rearm-overflow',
        feed_intent_revision=MAX_FEED_INTENT_REVISION,
        feed_intent_dispatched_revision=MAX_FEED_INTENT_REVISION,
    )

    with transaction.atomic():
        with pytest.raises(OverflowError, match='revision exhausted'):
            rearm_feed_intent(
                account.pk,
                MAX_FEED_INTENT_REVISION,
                timezone.now(),
            )

    account.refresh_from_db()
    assert account.feed_intent_revision == MAX_FEED_INTENT_REVISION
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
def test_nudge_only_moves_existing_undispatched_intent_earlier():
    account = _account(slug='nudge-undispatched')
    base = timezone.now()

    with transaction.atomic():
        assert nudge_undispatched_feed_intent(account.pk, base) is False

    MarketplaceAccount.all_objects.filter(pk=account.pk).update(
        feed_intent_revision=3,
        feed_intent_dispatched_revision=2,
        feed_intent_due_at=None,
    )
    with transaction.atomic():
        assert nudge_undispatched_feed_intent(account.pk, base) is True
    account.refresh_from_db()
    assert account.feed_intent_due_at == base

    with transaction.atomic():
        assert nudge_undispatched_feed_intent(
            account.pk,
            base + timedelta(minutes=5),
        ) is False
    account.refresh_from_db()
    assert account.feed_intent_due_at == base

    earlier = base - timedelta(minutes=5)
    with transaction.atomic():
        assert nudge_undispatched_feed_intent(account.pk, earlier) is True
    account.refresh_from_db()
    assert account.feed_intent_due_at == earlier


@pytest.mark.django_db
def test_feed_intent_primitives_do_not_touch_generic_account_updated_at():
    account = _account(slug='updated-at-stable')
    endpoint = _endpoint(account)
    original_updated_at = account.updated_at
    original_endpoint_updated_at = endpoint.updated_at
    due_at = timezone.now()

    with transaction.atomic():
        bump_feed_intents([account.pk], due_at)
    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.updated_at == original_updated_at
    assert endpoint.updated_at == original_endpoint_updated_at
    assert endpoint.source_intent_revision == 1

    MarketplaceAccount.all_objects.filter(pk=account.pk).update(
        feed_intent_dispatched_revision=1,
        feed_intent_due_at=None,
    )
    with transaction.atomic():
        assert rearm_feed_intent(account.pk, 1, None) == 2
    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.updated_at == original_updated_at
    assert endpoint.updated_at == original_endpoint_updated_at
    assert endpoint.source_intent_revision == 2

    with transaction.atomic():
        assert nudge_undispatched_feed_intent(account.pk, due_at) is True
    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.updated_at == original_updated_at
    assert endpoint.updated_at == original_endpoint_updated_at
    assert endpoint.source_intent_revision == 2


@pytest.mark.django_db
def test_feed_intent_primitives_reject_naive_datetimes_before_writes():
    account = _account(slug='aware-datetime')
    naive = timezone.now().replace(tzinfo=None)

    with transaction.atomic():
        with pytest.raises(ValueError, match='timezone-aware'):
            bump_feed_intents([account.pk], naive)
        with pytest.raises(ValueError, match='timezone-aware'):
            rearm_feed_intent(account.pk, 0, naive)
        with pytest.raises(ValueError, match='timezone-aware'):
            nudge_undispatched_feed_intent(account.pk, naive)

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None
