import io
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from apps.image_search.services.pipeline import _finalize_candidate_image
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product, ProductImage
from apps.products.storage import PhotoUploadPipeline
from apps.tenants.tests.auth import create_tenant_with_operator_key


def _feed_product(slug: str, *, account_active: bool = True):
    tenant, _api_key = create_tenant_with_operator_key(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Account {slug}',
        external_id=f'external-{slug}',
        credentials_enc=b'opaque-test-credentials',
        is_active=account_active,
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'IMAGE-{slug}',
        name=f'Image product {slug}',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        status=Listing.STATUS_ACTIVE,
        price_on_listing=Decimal('100.00'),
    )
    return product, account


def _jpeg_response():
    payload = io.BytesIO()
    Image.new('RGB', (640, 480), 'white').save(payload, format='JPEG')
    response = MagicMock()
    response.content = payload.getvalue()
    response.headers = {'Content-Type': 'image/jpeg'}
    return response


def _storage():
    storage = MagicMock()
    storage.save.side_effect = [
        'products/automatic/original.jpg',
        'products/automatic/thumb.jpg',
    ]
    return storage


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_publishable_automatic_image_prepares_storage_then_bumps_once_in_lock_order():
    from apps.products import feed_writers

    product, account = _feed_product('automatic-publishable')
    response = _jpeg_response()
    storage = _storage()
    events = []
    saved_names = (
        'products/automatic/original.jpg',
        'products/automatic/thumb.jpg',
    )
    original_writer = feed_writers.locked_product_images_feed_write

    def save(key, content):
        events.append('storage')
        return saved_names[len(events) - 1]

    storage.save.side_effect = save

    @contextmanager
    def traced_writer(*args, **kwargs):
        events.append('fence')
        with original_writer(*args, **kwargs) as locked:
            yield locked

    with (
        patch('apps.products.storage.request_public_http_url', return_value=response),
        patch.object(feed_writers, 'locked_product_images_feed_write', traced_writer),
        CaptureQueriesContext(connection) as queries,
    ):
        image = PhotoUploadPipeline(storage=storage).process(
            'https://images.example.test/part.jpg',
            product,
            status=ProductImage.Status.IMPORTED,
        )

    assert image is not None
    assert image.status == ProductImage.Status.IMPORTED
    assert events == ['storage', 'storage', 'fence']
    account.refresh_from_db()
    assert account.feed_intent_revision == 1

    locking_sql = [
        query['sql'].lower()
        for query in queries.captured_queries
        if 'for update' in query['sql'].lower()
    ]
    account_lock = next(
        index for index, sql in enumerate(locking_sql)
        if 'marketplaces_marketplaceaccount' in sql
    )
    product_lock = next(
        index for index, sql in enumerate(locking_sql)
        if 'products_product"' in sql
    )
    image_lock = next(
        index for index, sql in enumerate(locking_sql)
        if 'products_productimage' in sql
    )
    assert account_lock < product_lock < image_lock


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_nonpublishable_automatic_image_does_not_bump_feed_intent():
    product, account = _feed_product('automatic-review')

    with patch(
        'apps.products.storage.request_public_http_url',
        return_value=_jpeg_response(),
    ):
        image = PhotoUploadPipeline(storage=_storage()).process(
            'https://images.example.test/review.jpg',
            product,
            status=ProductImage.Status.NEEDS_REVIEW,
        )

    assert image is not None
    assert image.status == ProductImage.Status.NEEDS_REVIEW
    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='legacy')
def test_publishable_automatic_image_is_legacy_inert():
    product, account = _feed_product('automatic-legacy')

    with patch(
        'apps.products.storage.request_public_http_url',
        return_value=_jpeg_response(),
    ):
        image = PhotoUploadPipeline(storage=_storage()).process(
            'https://images.example.test/legacy.jpg',
            product,
            status=ProductImage.Status.IMPORTED,
        )

    assert image is not None
    account.refresh_from_db()
    assert account.feed_intent_revision == 0


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_publishable_automatic_image_records_paused_owner_intent():
    product, account = _feed_product(
        'automatic-paused',
        account_active=False,
    )

    with patch(
        'apps.products.storage.request_public_http_url',
        return_value=_jpeg_response(),
    ):
        image = PhotoUploadPipeline(storage=_storage()).process(
            'https://images.example.test/paused.jpg',
            product,
            status=ProductImage.Status.IMPORTED,
        )

    assert image is not None
    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_stale_image_generation_rolls_back_first_bump_and_reuses_prepared_objects():
    from apps.products import feed_writers

    product, account = _feed_product('automatic-stale')
    storage = _storage()
    original_capture = feed_writers.capture_product_image_feed_generations
    injected = False

    def raced_capture(product_id):
        nonlocal injected
        generation = original_capture(product_id)
        if not injected:
            ProductImage.objects.create(
                product_id=product_id,
                s3_key='products/automatic/concurrent.jpg',
                sha256='concurrent-image',
                status=ProductImage.Status.NEEDS_REVIEW,
            )
            injected = True
        return generation

    with (
        patch(
            'apps.products.storage.request_public_http_url',
            return_value=_jpeg_response(),
        ),
        patch.object(
            feed_writers,
            'capture_product_image_feed_generations',
            raced_capture,
        ),
    ):
        image = PhotoUploadPipeline(storage=storage).process(
            'https://images.example.test/stale.jpg',
            product,
            status=ProductImage.Status.IMPORTED,
        )

    assert injected is True
    assert image is not None
    assert ProductImage.objects.filter(product=product).count() == 2
    assert storage.save.call_count == 2
    storage.delete.assert_not_called()
    account.refresh_from_db()
    assert account.feed_intent_revision == 1


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_database_failure_rolls_back_intent_and_cleans_prepared_objects():
    product, account = _feed_product('automatic-rollback')
    storage = _storage()

    with (
        patch(
            'apps.products.storage.request_public_http_url',
            return_value=_jpeg_response(),
        ),
        patch.object(
            ProductImage.objects,
            'create',
            side_effect=RuntimeError('abort image insert'),
        ),
        pytest.raises(RuntimeError, match='abort image insert'),
    ):
        PhotoUploadPipeline(storage=storage).process(
            'https://images.example.test/rollback.jpg',
            product,
            status=ProductImage.Status.IMPORTED,
        )

    assert storage.delete.call_count == 2
    assert not ProductImage.objects.filter(product=product).exists()
    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_provider_finalization_never_downgrades_human_approved_image():
    product, account = _feed_product('automatic-finalize-race')
    image = ProductImage.objects.create(
        product=product,
        s3_key='products/automatic/approved.jpg',
        sha256='approved-image',
        source_id='manual-review',
        status=ProductImage.Status.AUTO_APPROVED,
    )

    finalized = _finalize_candidate_image(
        image.pk,
        source_id='provider',
        tier=3,
        quality_score=0.1,
        search_confidence='low',
        low_quality=True,
    )

    assert finalized is not None
    finalized.refresh_from_db()
    assert finalized.status == ProductImage.Status.AUTO_APPROVED
    assert finalized.source_id == 'manual-review'
    account.refresh_from_db()
    assert account.feed_intent_revision == 0
