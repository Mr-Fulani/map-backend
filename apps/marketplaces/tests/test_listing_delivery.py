from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.marketplaces.listing_delivery import listing_delivery_presentation
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.marketplaces.serializers import ListingSerializer
from apps.marketplaces.services import InvalidListingStatus, ListingService
from apps.products.models import Product
from apps.products.serializers import ProductDetailSerializer
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _durable_fleet(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'durable'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'active'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'stable_bridge'
    settings.MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED = False
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = []


def _listing(suffix: str) -> Listing:
    tenant = Tenant.objects.create(
        name=f'Delivery {suffix}',
        slug=f'delivery-{suffix}',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Avito',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=f'avito-{suffix}',
        credentials_enc=b'opaque-test-credentials',
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'DELIVERY-{suffix}',
        name=f'Delivery product {suffix}',
        price=Decimal('1000.00'),
    )
    return Listing.objects.create(
        tenant=tenant,
        account=account,
        product=product,
        status=Listing.STATUS_PENDING,
        price_on_listing=Decimal('1100.00'),
    )


def _feed_run(listing: Listing, state: str) -> MarketplaceFeedRun:
    submitted = state in {
        MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        MarketplaceFeedRun.State.POLLING,
        MarketplaceFeedRun.State.REPORTING,
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    }
    run = MarketplaceFeedRun.objects.create(
        tenant=listing.tenant,
        account=listing.account,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        state=state,
        account_identity_digest='a' * 64,
        submitted_at=timezone.now() if submitted else None,
    )
    listing.feed_run = run
    listing.save(update_fields=['feed_run'])
    return run


def test_pending_without_run_is_truthfully_shown_as_local_preparation():
    listing = _listing('local')

    data = ListingSerializer(listing).data

    assert data['status'] == Listing.STATUS_PENDING
    assert data['status_display'] == 'Готовится к отправке в Avito'
    assert data['delivery_stage'] == 'awaiting_feed'
    assert data['provider_submission_started'] is False
    assert data['lifecycle_actions_blocked'] is False
    assert data['can_check_avito_status'] is False


def test_product_listing_option_uses_the_same_truthful_delivery_label():
    listing = _listing('product-option')

    options = ProductDetailSerializer().get_listing_options(listing.product)

    assert options[0]['status'] == Listing.STATUS_PENDING
    assert options[0]['status_display'] == 'Готовится к отправке в Avito'


def test_preparing_run_is_not_presented_as_provider_processing():
    listing = _listing('preparing')
    _feed_run(listing, MarketplaceFeedRun.State.PREPARING)

    delivery = listing_delivery_presentation(listing)

    assert delivery.stage == 'feed_preparing'
    assert delivery.label == 'Фид готовится к отправке'
    assert delivery.provider_submission_started is False
    assert delivery.lifecycle_actions_blocked is True


def test_preparing_retry_exposes_delay_and_next_attempt():
    listing = _listing('preparing-retry')
    run = _feed_run(listing, MarketplaceFeedRun.State.PREPARING)
    retry_at = timezone.now() + timedelta(minutes=30)
    MarketplaceFeedRun.objects.filter(pk=run.pk).update(
        last_error='provider_baseline_read: temporary provider timeout',
        next_attempt_at=retry_at,
    )
    listing.refresh_from_db()

    data = ListingSerializer(listing).data

    assert data['status_display'] == 'Отправка временно задержана, повторяем'
    assert data['delivery_stage'] == 'delivery_retry'
    assert parse_datetime(data['delivery_retry_at']) == retry_at
    assert data['delivery_retry_reason'] == (
        'Avito временно не вернул состояние предыдущей автозагрузки.'
    )
    assert data['provider_submission_started'] is False
    assert data['lifecycle_actions_blocked'] is True
    assert data['can_check_avito_status'] is False


def test_polling_run_is_presented_as_avito_processing_and_locked():
    listing = _listing('polling')
    _feed_run(listing, MarketplaceFeedRun.State.POLLING)

    data = ListingSerializer(listing).data

    assert data['status_display'] == 'Avito обрабатывает фид'
    assert data['delivery_stage'] == 'avito_processing'
    assert data['provider_submission_started'] is True
    assert data['lifecycle_actions_blocked'] is True
    assert data['can_check_avito_status'] is True


def test_uncertain_run_requires_manual_review_and_disables_noop_check():
    listing = _listing('uncertain')
    _feed_run(listing, MarketplaceFeedRun.State.OUTCOME_UNCERTAIN)

    delivery = listing_delivery_presentation(listing)

    assert delivery.stage == 'manual_review'
    assert delivery.label == 'Результат Avito требует ручной проверки'
    assert delivery.lifecycle_actions_blocked is True
    assert delivery.can_check_avito_status is False


def test_failed_run_is_visible_and_no_longer_lifecycle_locked():
    listing = _listing('failed')
    _feed_run(listing, MarketplaceFeedRun.State.FAILED)

    delivery = listing_delivery_presentation(listing)

    assert delivery.stage == 'delivery_failed'
    assert delivery.label == 'Ошибка отправки в Avito'
    assert delivery.lifecycle_actions_blocked is False


@pytest.mark.parametrize('operation', ['archive', 'delete', 'account_move'])
def test_provider_owned_pending_listing_blocks_destructive_lifecycle(operation):
    listing = _listing(f'blocked-{operation}')
    run = _feed_run(listing, MarketplaceFeedRun.State.POLLING)
    replacement = MarketplaceAccount.objects.create(
        tenant=listing.tenant,
        name='Replacement',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=f'replacement-{operation}',
        credentials_enc=b'opaque-replacement-credentials',
    )

    with pytest.raises(InvalidListingStatus, match='текущая отправка'):
        if operation == 'archive':
            ListingService.archive(listing.pk, listing.tenant)
        elif operation == 'delete':
            ListingService.delete(listing.pk, listing.tenant)
        else:
            ListingService.update_listing_fields(
                listing.pk,
                listing.tenant,
                {'account_id': replacement.pk},
            )

    listing.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING
    assert listing.account_id != replacement.pk
    assert listing.feed_run_id == run.pk


def test_preparing_generation_blocks_archive_before_worker_can_submit_it():
    listing = _listing('preparing-race')
    run = _feed_run(listing, MarketplaceFeedRun.State.PREPARING)

    with pytest.raises(InvalidListingStatus, match='текущая отправка'):
        ListingService.archive(listing.pk, listing.tenant)

    listing.refresh_from_db()
    run.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING
    assert listing.feed_run_id == run.pk
    assert run.state == MarketplaceFeedRun.State.PREPARING


def test_local_pending_without_run_can_be_cancelled_before_submission():
    listing = _listing('cancel-local')

    with patch('apps.marketplaces.services._enqueue_unpublish'):
        result = ListingService.archive(listing.pk, listing.tenant)

    listing.refresh_from_db()
    assert result.status == Listing.STATUS_ARCHIVING
    assert listing.status == Listing.STATUS_ARCHIVING


def test_legacy_pending_without_generation_fails_closed(settings):
    listing = _listing('legacy')
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'

    delivery = listing_delivery_presentation(listing)
    assert delivery.stage == 'legacy_delivery'
    assert delivery.lifecycle_actions_blocked is True

    with pytest.raises(InvalidListingStatus, match='текущая отправка'):
        ListingService.delete(listing.pk, listing.tenant)

    listing.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING


def test_manual_check_is_rejected_until_provider_submission_starts():
    listing = _listing('check-local')

    with pytest.raises(InvalidListingStatus, match='готовится к отправке'):
        ListingService.check_avito_status(listing.pk, listing.tenant)


def test_manual_check_is_available_for_polling_run():
    listing = _listing('check-provider')
    _feed_run(listing, MarketplaceFeedRun.State.POLLING)

    with patch('apps.marketplaces.services._enqueue_poll_feed_results'):
        result = ListingService.check_avito_status(listing.pk, listing.tenant)

    assert result.pk == listing.pk
