from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.marketplaces.adapters.avito.adapter import FeedUploadError
from apps.marketplaces.adapters.avito.feed_builder import get_ad_id
from apps.marketplaces.feed_intents import bump_feed_intents
from apps.marketplaces.feed_report_reconciler import (
    _account_identity_marker,
    _apply_page_errors,
    _flush_marker,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    MarketplaceFeedEndpoint,
)
from apps.products.models import Product
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _dual_write_modes(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'


def _listing(
    suffix: str,
    *,
    status: str,
    external_id: str | None = 'provider-listing-id',
) -> tuple[Listing, MarketplaceFeedEndpoint]:
    tenant = Tenant.objects.create(
        name=f'Provider result {suffix}',
        slug=f'provider-result-{suffix}',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Avito',
        external_id=f'provider-account-{suffix}',
        credentials_enc=b'opaque-test-credentials',
        last_feed_flush_at=timezone.now(),
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=account_identity_digest(account),
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'PROVIDER-{suffix}',
        name=f'Provider result product {suffix}',
        price=Decimal('1000.00'),
    )
    listing = Listing.objects.create(
        tenant=tenant,
        account=account,
        product=product,
        status=status,
        external_id=external_id,
        price_on_listing=Decimal('1100.00'),
    )
    return listing, endpoint


def _assert_feed_revision(
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
    expected: int,
) -> None:
    account.refresh_from_db()
    endpoint.refresh_from_db()
    assert account.feed_intent_revision == expected
    assert endpoint.source_intent_revision == expected


def test_claimed_active_pending_transitions_preserve_identity_without_bump():
    from apps.marketplaces.tasks import (
        _apply_claimed_status_result,
        _claim_listing_status_check,
    )

    listing, endpoint = _listing(
        'active-pending',
        status=Listing.STATUS_ACTIVE,
    )
    provider_identity = (
        listing.external_id,
        listing.publish_idempotency_key,
    )

    for source_status, target_status in (
        (Listing.STATUS_ACTIVE, Listing.STATUS_PENDING),
        (Listing.STATUS_PENDING, Listing.STATUS_ACTIVE),
    ):
        claim, reason = _claim_listing_status_check(
            listing.pk,
            eligible_statuses=(source_status,),
            require_external_id=True,
        )
        assert reason == ''
        assert claim is not None
        checked_at = timezone.now()
        assert _apply_claimed_status_result(
            claim,
            raw_remote_status='processing',
            checked_at=checked_at,
            next_status_check_at=None,
            canonical_updates={
                'status': target_status,
                'last_sync_at': checked_at,
            },
        ) == 1
        listing.refresh_from_db()
        assert listing.status == target_status
        assert (
            listing.external_id,
            listing.publish_idempotency_key,
        ) == provider_identity

    _assert_feed_revision(listing.account, endpoint, 0)


@pytest.mark.parametrize(
    ('suffix', 'initial_status', 'provider_payload', 'expected_status'),
    [
        (
            'moderation-reject',
            Listing.STATUS_ACTIVE,
            {'status': 'rejected', 'rejection_reason': 'provider rejected'},
            Listing.STATUS_REJECTED,
        ),
        (
            'moderation-restore',
            Listing.STATUS_REJECTED,
            {'status': 'active'},
            Listing.STATUS_ACTIVE,
        ),
        (
            'moderation-archive',
            Listing.STATUS_ACTIVE,
            {'status': 'old'},
            Listing.STATUS_ARCHIVED,
        ),
    ],
)
def test_moderation_projection_membership_boundary_bumps_once(
    suffix,
    initial_status,
    provider_payload,
    expected_status,
):
    from apps.marketplaces.tasks import check_moderation_task

    listing, endpoint = _listing(suffix, status=initial_status)
    provider_identity = (
        listing.external_id,
        listing.publish_idempotency_key,
    )
    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value=provider_payload,
        ),
        patch('apps.marketplaces.tasks._notify_error'),
        patch('apps.marketplaces.tasks._write_log'),
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    assert result['changed'] is True
    assert listing.status == expected_status
    assert (
        listing.external_id,
        listing.publish_idempotency_key,
    ) == provider_identity
    _assert_feed_revision(listing.account, endpoint, 1)


def test_confirmed_unpublish_does_not_double_bump_after_projection_exit():
    from apps.marketplaces.tasks import confirm_removal_task

    listing, endpoint = _listing(
        'confirm-unpublish',
        status=Listing.STATUS_ARCHIVING,
    )
    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value={'status': 'old'},
        ),
        patch('apps.marketplaces.tasks._write_log'),
    ):
        result = confirm_removal_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'archived', 'changed': True}
    assert listing.status == Listing.STATUS_ARCHIVED
    _assert_feed_revision(listing.account, endpoint, 0)


def test_feed_poll_rejection_bumps_once_but_pending_publish_does_not():
    from apps.marketplaces.tasks import poll_feed_results_task

    rejected, rejected_endpoint = _listing(
        'poll-rejected',
        status=Listing.STATUS_PENDING,
        external_id=None,
    )
    rejected_identity = rejected.publish_idempotency_key
    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            side_effect=FeedUploadError('provider feed rejection'),
        ),
        patch('apps.marketplaces.tasks._notify_error'),
        patch('apps.marketplaces.tasks._write_log'),
    ):
        poll_feed_results_task(rejected.account_id)

    rejected.refresh_from_db()
    assert rejected.status == Listing.STATUS_REJECTED
    assert rejected.publish_idempotency_key == rejected_identity
    _assert_feed_revision(rejected.account, rejected_endpoint, 1)

    published, published_endpoint = _listing(
        'poll-published',
        status=Listing.STATUS_PENDING,
        external_id=None,
    )
    published_identity = published.publish_idempotency_key
    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            return_value=[{
                'ad_id': get_ad_id(published),
                'avito_id': 'assigned-provider-id',
            }],
        ),
        patch('apps.marketplaces.tasks._notify_success'),
        patch('apps.marketplaces.tasks._write_log'),
    ):
        poll_feed_results_task(published.account_id)

    published.refresh_from_db()
    assert published.status == Listing.STATUS_ACTIVE
    assert published.external_id == 'assigned-provider-id'
    assert published.publish_idempotency_key == published_identity
    _assert_feed_revision(published.account, published_endpoint, 0)


def test_claimed_result_cas_miss_rolls_back_speculative_feed_bump():
    from apps.marketplaces.tasks import (
        _apply_claimed_status_result,
        _claim_listing_status_check,
    )

    listing, endpoint = _listing(
        'cas-rollback',
        status=Listing.STATUS_ACTIVE,
    )
    claim, reason = _claim_listing_status_check(
        listing.pk,
        eligible_statuses=(Listing.STATUS_ACTIVE,),
        require_external_id=True,
    )
    assert reason == ''
    assert claim is not None

    def bump_then_revoke_claim(account_ids, observed_at):
        revisions = bump_feed_intents(account_ids, observed_at)
        Listing.objects.filter(pk=listing.pk).update(
            status=Listing.STATUS_ARCHIVING,
            status_check_claim_token=None,
            status_check_claimed_until=None,
        )
        return revisions

    checked_at = timezone.now()
    with patch(
        'apps.marketplaces.feed_intents.bump_feed_intents',
        side_effect=bump_then_revoke_claim,
    ):
        affected = _apply_claimed_status_result(
            claim,
            raw_remote_status='rejected',
            checked_at=checked_at,
            next_status_check_at=None,
            canonical_updates={
                'status': Listing.STATUS_REJECTED,
                'last_sync_at': checked_at,
            },
        )

    listing.refresh_from_db()
    assert affected == 0
    assert listing.status == Listing.STATUS_ACTIVE
    assert listing.status_check_claim_token == claim.claim_token
    _assert_feed_revision(listing.account, endpoint, 0)


def test_report_page_batch_bumps_once_and_redelivery_is_idempotent():
    first, endpoint = _listing(
        'report-batch-first',
        status=Listing.STATUS_PENDING,
        external_id=None,
    )
    product = Product.objects.create(
        tenant=first.tenant,
        article='PROVIDER-report-batch-second',
        name='Provider result product report batch second',
        price=Decimal('1000.00'),
    )
    second = Listing.objects.create(
        tenant=first.tenant,
        account=first.account,
        product=product,
        status=Listing.STATUS_PENDING,
        external_id=None,
        price_on_listing=Decimal('1100.00'),
    )
    identities = {
        first.pk: first.publish_idempotency_key,
        second.pk: second.publish_idempotency_key,
    }
    errors = {
        str(first.publish_idempotency_key): 'first provider error',
        str(second.publish_idempotency_key): 'second provider error',
    }
    kwargs = {
        'account_id': first.account_id,
        'tenant_id': first.tenant_id,
        'expected_flush_marker': _flush_marker(first.account),
        'expected_account_marker': _account_identity_marker(first.account),
        'errors': errors,
    }

    applied = _apply_page_errors(**kwargs)

    first.refresh_from_db()
    second.refresh_from_db()
    assert applied.changed_count == 2
    assert first.status == Listing.STATUS_REJECTED
    assert second.status == Listing.STATUS_REJECTED
    assert first.publish_idempotency_key == identities[first.pk]
    assert second.publish_idempotency_key == identities[second.pk]
    _assert_feed_revision(first.account, endpoint, 1)

    redelivery = _apply_page_errors(**kwargs)
    assert redelivery.changed_count == 0
    _assert_feed_revision(first.account, endpoint, 1)
