from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.marketplaces.listing_delivery import listing_delivery_presentation
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.marketplaces.serializers import ListingSerializer
from apps.marketplaces.services import (
    InvalidListingStatus,
    ListingPublicationValidationError,
    ListingService,
)
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
    assert delivery.lifecycle_actions_blocked is False


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
    assert data['lifecycle_actions_blocked'] is False
    assert data['can_check_avito_status'] is False


def test_pending_content_edit_during_preparation_creates_successor_revision():
    listing = _listing('preparing-edit')
    run = _feed_run(listing, MarketplaceFeedRun.State.PREPARING)

    ListingService.update_content(
        listing.pk,
        listing.tenant,
        'Актуальный заголовок',
        'Актуальное описание',
    )

    listing.refresh_from_db()
    listing.account.refresh_from_db()
    run.refresh_from_db()
    assert listing.title == 'Актуальный заголовок'
    assert listing.description_ai == 'Актуальное описание'
    assert listing.feed_run_id == run.pk
    assert listing.account.feed_intent_revision == 1
    assert listing.account.feed_intent_due_at is not None
    assert run.state == MarketplaceFeedRun.State.PREPARING


def test_polling_run_is_presented_as_avito_processing_with_successor_edits_allowed():
    listing = _listing('polling')
    _feed_run(listing, MarketplaceFeedRun.State.POLLING)

    data = ListingSerializer(listing).data

    assert data['status_display'] == 'Avito обрабатывает фид'
    assert data['delivery_stage'] == 'avito_processing'
    assert data['provider_submission_started'] is True
    assert data['lifecycle_actions_blocked'] is False
    assert data['can_check_avito_status'] is True


def test_ambiguous_submission_blocks_destructive_lifecycle_until_reconciled():
    listing = _listing('submit-unknown')
    _feed_run(listing, MarketplaceFeedRun.State.SUBMIT_UNKNOWN)

    delivery = listing_delivery_presentation(listing)

    assert delivery.stage == 'submission_unknown'
    assert delivery.provider_submission_started is True
    assert delivery.lifecycle_actions_blocked is True


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

    data = ListingSerializer(listing).data
    assert data['can_publish'] is True


def test_failed_pre_submission_run_can_be_retried_with_fresh_generation(
    django_capture_on_commit_callbacks,
):
    listing = _listing('failed-retry')
    listing.title = 'Исправный заголовок'
    listing.description_ai = 'Исправное описание'
    listing.rejection_reason = 'Ошибка прошлой попытки'
    listing.save(update_fields=['title', 'description_ai', 'rejection_reason'])
    listing.product.brand = 'Bosch'
    listing.product.condition = 'used'
    listing.product.save(update_fields=['brand', 'condition'])
    listing.account.default_manager_name = 'Менеджер'
    listing.account.default_contact_phone = '+79990000000'
    listing.account.default_address = 'Москва, Загородное шоссе, 1'
    listing.account.save(update_fields=[
        'default_manager_name',
        'default_contact_phone',
        'default_address',
    ])
    failed_run = _feed_run(listing, MarketplaceFeedRun.State.FAILED)

    with patch('apps.marketplaces.services._enqueue_publish_or_update') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            result = ListingService.publish(listing.pk, listing.tenant)

    result.refresh_from_db()
    assert result.status == Listing.STATUS_QUEUED
    assert result.feed_run_id is None
    assert result.rejection_reason == ''
    assert MarketplaceFeedRun.objects.filter(pk=failed_run.pk).exists()
    enqueue.assert_called_once_with(listing.pk, is_new=True)


def test_unknown_provider_outcome_never_exposes_blind_retry():
    listing = _listing('unknown-no-retry')
    _feed_run(listing, MarketplaceFeedRun.State.SUBMIT_UNKNOWN)

    data = ListingSerializer(listing).data
    assert data['can_publish'] is False
    with pytest.raises(InvalidListingStatus, match='текущая стадия: submission_unknown'):
        ListingService.publish(listing.pk, listing.tenant)


def test_failed_retry_keeps_terminal_evidence_until_fields_are_valid():
    listing = _listing('failed-invalid-fields')
    listing.title = 'Исправный заголовок'
    listing.description_ai = 'Исправное описание'
    listing.rejection_reason = 'Ошибка прошлой попытки'
    listing.save(update_fields=['title', 'description_ai', 'rejection_reason'])
    listing.product.condition = 'used'
    listing.product.save(update_fields=['condition'])
    failed_run = _feed_run(listing, MarketplaceFeedRun.State.FAILED)

    with pytest.raises(ListingPublicationValidationError) as error:
        ListingService.publish(listing.pk, listing.tenant)

    assert set(error.value.field_errors) >= {
        'manager_name_override',
        'contact_phone_override',
    }
    listing.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING
    assert listing.feed_run_id == failed_run.pk
    assert listing.rejection_reason == 'Ошибка прошлой попытки'


def test_failed_row_with_submission_evidence_never_exposes_retry():
    listing = _listing('failed-with-submission-evidence')
    failed_run = _feed_run(listing, MarketplaceFeedRun.State.FAILED)
    failed_run.submitted_at = timezone.now()
    failed_run.save(update_fields=['submitted_at'])
    listing.refresh_from_db()

    data = ListingSerializer(listing).data
    assert data['can_publish'] is False
    assert data['delivery_stage'] == 'manual_review'
    assert data['lifecycle_actions_blocked'] is True
    with pytest.raises(InvalidListingStatus, match='текущая стадия: manual_review'):
        ListingService.publish(listing.pk, listing.tenant)


@pytest.mark.parametrize(
    'state',
    [
        MarketplaceFeedRun.State.PREPARING,
        MarketplaceFeedRun.State.POLLING,
        MarketplaceFeedRun.State.REPORTING,
        MarketplaceFeedRun.State.RETRY_WAIT,
    ],
)
@pytest.mark.parametrize('operation', ['archive', 'delete', 'account_move'])
def test_known_generation_accepts_successor_lifecycle_intent(state, operation):
    listing = _listing(f'successor-{state}-{operation}')
    run = _feed_run(listing, state)
    replacement = MarketplaceAccount.objects.create(
        tenant=listing.tenant,
        name='Replacement',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=f'replacement-{state}-{operation}',
        credentials_enc=b'opaque-replacement-credentials',
    )

    with patch('apps.marketplaces.services.transaction.on_commit'):
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
    listing.account.refresh_from_db()
    if operation == 'archive':
        assert listing.status == Listing.STATUS_ARCHIVING
        assert listing.account.feed_intent_revision == 1
        assert listing.feed_run_id == run.pk
    elif operation == 'delete':
        assert listing.status == Listing.STATUS_DELETED
        assert listing.account.feed_intent_revision == 1
        assert listing.feed_run_id == run.pk
    else:
        original_account = MarketplaceAccount.all_objects.get(pk=run.account_id)
        replacement.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        assert listing.account_id == replacement.pk
        assert listing.feed_run_id is None
        assert original_account.feed_intent_revision == 1
        assert replacement.feed_intent_revision == 1


@pytest.mark.parametrize(
    'state',
    [
        MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    ],
)
@pytest.mark.parametrize('operation', ['archive', 'delete', 'account_move'])
def test_unknown_provider_outcome_blocks_destructive_lifecycle(state, operation):
    listing = _listing(f'blocked-{state}-{operation}')
    run = _feed_run(listing, state)
    replacement = MarketplaceAccount.objects.create(
        tenant=listing.tenant,
        name='Replacement',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=f'blocked-replacement-{state}-{operation}',
        credentials_enc=b'opaque-replacement-credentials',
    )

    with pytest.raises(InvalidListingStatus, match='неизвестно, принял ли'):
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
    run.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING
    assert listing.account_id != replacement.pk
    assert listing.feed_run_id == run.pk
    assert run.state == state


def test_local_pending_without_run_can_be_cancelled_before_submission():
    listing = _listing('cancel-local')

    with patch('apps.marketplaces.services._enqueue_unpublish'):
        result = ListingService.archive(listing.pk, listing.tenant)

    listing.refresh_from_db()
    assert result.status == Listing.STATUS_ARCHIVING
    assert listing.status == Listing.STATUS_ARCHIVING


def test_legacy_pending_without_unknown_boundary_keeps_old_lifecycle_behavior(settings):
    listing = _listing('legacy')
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'

    delivery = listing_delivery_presentation(listing)
    assert delivery.stage == 'legacy_delivery'
    assert delivery.lifecycle_actions_blocked is False

    with patch('apps.marketplaces.services.transaction.on_commit'):
        ListingService.delete(listing.pk, listing.tenant)

    listing.refresh_from_db()
    assert listing.status == Listing.STATUS_DELETED


def test_legacy_unknown_provider_boundary_still_fails_closed(settings):
    listing = _listing('legacy-unknown')
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'
    MarketplaceAccount.objects.filter(pk=listing.account_id).update(
        feed_intent_revision=1,
        feed_intent_dispatched_revision=0,
        feed_intent_due_at=None,
    )
    listing.refresh_from_db()

    delivery = listing_delivery_presentation(listing)
    assert delivery.stage == 'legacy_delivery'
    assert delivery.lifecycle_actions_blocked is True

    with pytest.raises(InvalidListingStatus, match='неизвестно, принял ли'):
        ListingService.delete(listing.pk, listing.tenant)


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
