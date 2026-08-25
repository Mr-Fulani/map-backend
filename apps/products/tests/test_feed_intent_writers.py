import json
import io
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.models import SoftDeleteQuerySet
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.media_processing.models import ProductImageVariant
from apps.media_processing.services import activate_variant
from apps.products.feed_writers import (
    StaleProductFeedWrite,
    capture_product_feed_generations,
    locked_product_feed_write,
)
from apps.products.models import Product, ProductImage, TenantCatalogCategory
from apps.products.services import ProductEnrichmentService, ProductService
from apps.tenants.models import CatalogDomain, TenantCatalogDomain
from apps.tenants.tests.auth import create_tenant_with_operator_key


def _tenant(slug: str):
    return create_tenant_with_operator_key(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )


def _datasource(tenant, slug: str) -> DataSourceConnection:
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name=f'Source {slug}',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({
            'url': 'https://source.example.test',
            'user': 'user',
            'password': 'secret',
        }),
    )


def _account(tenant, slug: str, **values) -> MarketplaceAccount:
    defaults = {
        'tenant': tenant,
        'marketplace': MarketplaceAccount.MARKETPLACE_AVITO,
        'name': f'Account {slug}',
        'external_id': f'external-{slug}',
        'credentials_enc': b'opaque-test-credentials',
    }
    defaults.update(values)
    return MarketplaceAccount.objects.create(**defaults)


def _source_item(article: str, **values) -> dict:
    item = {
        'uuid': None,
        'article': article,
        'name': f'Part {article}',
        'brand': 'Brand',
        'price': '100.00',
        'stock_qty': 3,
        'category': 'Brakes',
        'condition': 'new',
        'description': 'Source description',
    }
    item.update(values)
    return item


def _listing(product: Product, account: MarketplaceAccount, **values) -> Listing:
    defaults = {
        'tenant': product.tenant,
        'product': product,
        'account': account,
        'status': Listing.STATUS_ACTIVE,
        'price_on_listing': Decimal('100.00'),
    }
    defaults.update(values)
    return Listing.objects.create(**defaults)


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_source_content_change_bumps_each_live_account_once():
    tenant, _ = _tenant('feed-product-content')
    datasource = _datasource(tenant, 'content')
    product, _, _ = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('CONTENT-1'),
    )
    first = _account(tenant, 'content-first')
    second = _account(tenant, 'content-second')
    _listing(product, first)
    _listing(product, second, status=Listing.STATUS_PENDING)

    product, result, change_type = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('CONTENT-1', name='Changed feed title'),
    )

    assert result == 'updated'
    assert change_type == 'content'
    assert product.name == 'Changed feed title'
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.feed_intent_revision == 1
    assert second.feed_intent_revision == 1


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_product_price_change_does_not_create_product_feed_false_positive():
    tenant, _ = _tenant('feed-product-price')
    datasource = _datasource(tenant, 'price')
    product, _, _ = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('PRICE-1'),
    )
    account = _account(tenant, 'price')
    _listing(product, account)

    _, result, change_type = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('PRICE-1', price='125.00'),
    )

    assert result == 'updated'
    assert change_type == 'price_only'
    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='legacy')
def test_legacy_product_content_writer_is_inert():
    tenant, _ = _tenant('feed-product-legacy')
    datasource = _datasource(tenant, 'legacy')
    product, _, _ = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('LEGACY-1'),
    )
    account = _account(tenant, 'legacy')
    _listing(product, account)

    ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('LEGACY-1', description='Changed XML description'),
    )

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_deleted_rows_are_ignored_but_paused_account_keeps_product_intent():
    tenant, _ = _tenant('feed-product-inactive')
    datasource = _datasource(tenant, 'inactive')
    product, _, _ = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('INACTIVE-1'),
    )
    deleted_listing_owner = _account(tenant, 'deleted-listing')
    deleted_account_owner = _account(tenant, 'deleted-account')
    paused_owner = _account(tenant, 'inactive-account')
    deleted_listing = _listing(product, deleted_listing_owner)
    _listing(product, deleted_account_owner)
    _listing(product, paused_owner)
    Listing.all_objects.filter(pk=deleted_listing.pk).update(deleted_at=timezone.now())
    MarketplaceAccount.objects.filter(pk=deleted_account_owner.pk).update(
        deleted_at=timezone.now(),
        is_active=False,
    )
    MarketplaceAccount.objects.filter(pk=paused_owner.pk).update(is_active=False)

    ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item('INACTIVE-1', brand='Changed Brand'),
    )

    deleted_listing_owner.refresh_from_db()
    deleted_account_owner.refresh_from_db()
    paused_owner.refresh_from_db()
    assert deleted_listing_owner.feed_intent_revision == 0
    assert deleted_account_owner.feed_intent_revision == 0
    assert paused_owner.feed_intent_revision == 1


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_product_delete_while_paused_survives_account_reactivation():
    from apps.marketplaces.services import MarketplaceAccountService

    tenant, _ = _tenant('feed-product-paused-delete')
    account = _account(tenant, 'paused-delete')
    product = Product.objects.create(
        tenant=tenant,
        article='PAUSED-DELETE-1',
        name='Paused delete product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)

    MarketplaceAccountService.update_partial(account, {'is_active': False})
    product.soft_delete()

    account.refresh_from_db()
    assert account.is_active is False
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None

    MarketplaceAccountService.update_partial(account, {'is_active': True})

    account.refresh_from_db()
    assert account.is_active is True
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_bulk_delete_of_500_products_bumps_one_revision_per_account():
    tenant, api_key = _tenant('feed-product-bulk-500')
    account = _account(tenant, 'bulk-500')
    products = Product.objects.bulk_create([
        Product(
            tenant=tenant,
            article=f'BULK-{index}',
            name=f'Bulk product {index}',
            price=Decimal('100.00'),
            stock_qty=1,
        )
        for index in range(500)
    ])
    Listing.objects.bulk_create([
        Listing(
            tenant=tenant,
            product=product,
            account=account,
            status=Listing.STATUS_ACTIVE,
            price_on_listing=Decimal('100.00'),
        )
        for product in products
    ])

    response = Client().delete(
        '/api/v1/products/bulk-delete/',
        data=json.dumps({'product_ids': [product.pk for product in products]}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    assert response.json()['data']['deleted_count'] == 500
    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert Product.objects.filter(pk__in=[product.pk for product in products]).count() == 0
    assert Listing.objects.filter(account=account).count() == 0


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_product_soft_delete_records_last_listing_before_it_is_hidden():
    tenant, _ = _tenant('feed-product-last-listing')
    account = _account(tenant, 'last-listing')
    product = Product.objects.create(
        tenant=tenant,
        article='LAST-1',
        name='Last product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    listing = _listing(product, account)

    product.soft_delete()

    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None
    assert not Product.objects.filter(pk=product.pk).exists()
    assert not Listing.objects.filter(pk=listing.pk).exists()


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_product_soft_delete_rolls_back_feed_cursor_and_hidden_rows():
    tenant, _ = _tenant('feed-product-delete-rollback')
    account = _account(tenant, 'delete-rollback')
    product = Product.objects.create(
        tenant=tenant,
        article='ROLLBACK-1',
        name='Rollback product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    listing = _listing(product, account)

    with patch.object(
        SoftDeleteQuerySet,
        'delete',
        side_effect=RuntimeError('abort product delete'),
    ):
        with pytest.raises(RuntimeError, match='abort product delete'):
            product.soft_delete()

    account.refresh_from_db()
    product.refresh_from_db()
    listing.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None
    assert product.deleted_at is None
    assert product.sync_excluded is False
    assert listing.deleted_at is None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_stale_product_generation_rolls_back_account_bump():
    tenant, _ = _tenant('feed-product-stale')
    account = _account(tenant, 'stale')
    product = Product.objects.create(
        tenant=tenant,
        article='STALE-1',
        name='Before',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)
    generation = capture_product_feed_generations((product.pk,))[product.pk]
    # QuerySet.update deliberately leaves updated_at unchanged. The exact
    # feed-field comparison must still detect this stale generation.
    Product.objects.filter(pk=product.pk).update(name='Concurrent writer')

    with pytest.raises(StaleProductFeedWrite, match='changed before'):
        with locked_product_feed_write((generation,)):
            raise AssertionError('stale writer must never enter its mutation body')

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_product_feed_writer_locks_account_before_product():
    tenant, _ = _tenant('feed-product-lock-order')
    account = _account(tenant, 'lock-order')
    product = Product.objects.create(
        tenant=tenant,
        article='LOCK-1',
        name='Lock order',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)
    generation = capture_product_feed_generations((product.pk,))[product.pk]

    with CaptureQueriesContext(connection) as queries:
        with locked_product_feed_write((generation,)):
            pass

    locking_sql = [
        query['sql']
        for query in queries.captured_queries
        if 'FOR UPDATE' in query['sql'].upper()
    ]
    account_lock = next(
        index for index, sql in enumerate(locking_sql)
        if 'marketplaces_marketplaceaccount' in sql.lower()
    )
    product_lock = next(
        index for index, sql in enumerate(locking_sql)
        if 'products_product' in sql.lower()
    )
    assert account_lock < product_lock


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_image_approval_and_rejection_record_projection_intents():
    from apps.image_search.services import moderation

    tenant, _ = _tenant('feed-product-image-review')
    account = _account(tenant, 'image-review')
    product = Product.objects.create(
        tenant=tenant,
        article='IMAGE-1',
        name='Image product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)
    image = ProductImage.objects.create(
        product=product,
        s3_key='products/image-review/source.jpg',
        sha256='image-review',
        status=ProductImage.Status.NEEDS_REVIEW,
    )

    image = moderation.approve(image)
    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert image.status == ProductImage.Status.AUTO_APPROVED

    image = moderation.reject(image)
    account.refresh_from_db()
    assert account.feed_intent_revision == 2
    assert image.status == ProductImage.Status.REJECTED


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_manual_image_upload_records_one_feed_intent():
    from PIL import Image

    from apps.image_search.services.moderation import upload_image

    tenant, _ = _tenant('feed-product-image-upload')
    account = _account(tenant, 'image-upload')
    product = Product.objects.create(
        tenant=tenant,
        article='IMAGE-UPLOAD-1',
        name='Image upload product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)
    payload = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(payload, format='JPEG')
    storage = MagicMock()
    storage.save.side_effect = [
        'products/image-upload/original.jpg',
        'products/image-upload/thumb.jpg',
    ]

    with patch('apps.image_search.services.moderation.default_storage', storage):
        image = upload_image(product, payload.getvalue())

    assert image is not None
    assert image.status == ProductImage.Status.MANUALLY_SET
    account.refresh_from_db()
    assert account.feed_intent_revision == 1


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_active_image_variant_switch_records_feed_intent():
    tenant, _ = _tenant('feed-product-variant')
    account = _account(tenant, 'variant')
    product = Product.objects.create(
        tenant=tenant,
        article='VARIANT-1',
        name='Variant product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)
    image = ProductImage.objects.create(
        product=product,
        s3_key='products/variant/source.jpg',
        sha256='variant-source',
        status=ProductImage.Status.IMPORTED,
    )
    first = ProductImageVariant.objects.create(
        tenant=tenant,
        product_image=image,
        s3_key='products/variant/first.jpg',
        sha256='variant-first',
        is_active=True,
    )
    second = ProductImageVariant.objects.create(
        tenant=tenant,
        product_image=image,
        s3_key='products/variant/second.jpg',
        sha256='variant-second',
    )

    activate_variant(second)

    first.refresh_from_db()
    second.refresh_from_db()
    account.refresh_from_db()
    assert first.is_active is False
    assert second.is_active is True
    assert account.feed_intent_revision == 1


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_parent_category_name_change_bumps_descendant_products_only_once():
    tenant, api_key = _tenant('feed-product-category-ancestry')
    root_domain = CatalogDomain.objects.get(slug='auto_parts')
    TenantCatalogDomain.objects.update_or_create(
        tenant=tenant,
        domain=root_domain,
        defaults={'is_enabled': True},
    )
    parent = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Old parent',
        root_domain=root_domain,
        domain=root_domain.slug,
        external_source='avito',
        external_id='old-parent',
    )
    child = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Child',
        parent=parent,
        root_domain=root_domain,
        domain=root_domain.slug,
        external_source='avito',
        external_id='child',
    )
    account = _account(tenant, 'category-ancestry')
    product = Product.objects.create(
        tenant=tenant,
        article='CATEGORY-1',
        name='Category product',
        catalog_category=child,
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)

    response = Client().patch(
        f'/api/v1/products/catalog-categories/{parent.pk}/',
        data=json.dumps({'name': 'New parent'}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    parent.refresh_from_db()
    account.refresh_from_db()
    assert parent.name == 'New parent'
    assert account.feed_intent_revision == 1


def _classification_product(tenant, account, suffix: str):
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name=f'Classified {suffix}',
        external_source='avito',
        external_id=f'classified-{suffix}',
    )
    from apps.products.models import TenantCategoryMapping

    TenantCategoryMapping.objects.create(
        tenant=tenant,
        source_category=f'Source {suffix}',
        category=category,
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'CLASSIFY-{suffix}',
        name=f'Classification product {suffix}',
        category_1c=f'Source {suffix}',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    _listing(product, account)
    return product, category


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_classification_category_assignment_records_live_product_intent():
    tenant, _ = _tenant('feed-product-classification')
    account = _account(tenant, 'classification')
    product, category = _classification_product(tenant, account, 'LIVE')

    ProductEnrichmentService.classify_product_catalog_domain(product)

    product.refresh_from_db()
    account.refresh_from_db()
    assert product.catalog_category_id == category.pk
    assert account.feed_intent_revision == 1


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='legacy')
def test_classification_category_assignment_is_legacy_inert():
    tenant, _ = _tenant('feed-product-classification-legacy')
    account = _account(tenant, 'classification-legacy')
    product, category = _classification_product(tenant, account, 'LEGACY')

    ProductEnrichmentService.classify_product_catalog_domain(product)

    product.refresh_from_db()
    account.refresh_from_db()
    assert product.catalog_category_id == category.pk
    assert account.feed_intent_revision == 0


@pytest.mark.django_db
@override_settings(MARKETPLACE_FEED_INGRESS_MODE='dual_write')
def test_classification_stale_generation_rolls_back_then_records_one_intent():
    from apps.products import feed_writers

    tenant, _ = _tenant('feed-product-classification-stale')
    account = _account(tenant, 'classification-stale')
    product, category = _classification_product(tenant, account, 'STALE')
    original_writer = feed_writers.locked_product_feed_write
    injected = False

    @contextmanager
    def raced_writer(generations, **kwargs):
        nonlocal injected
        if not injected:
            Product.objects.filter(pk=product.pk).update(name='Concurrent name')
            injected = True
        with original_writer(generations, **kwargs) as locked:
            yield locked

    with patch.object(feed_writers, 'locked_product_feed_write', raced_writer):
        ProductEnrichmentService.classify_product_catalog_domain(product)

    product.refresh_from_db()
    account.refresh_from_db()
    assert injected is True
    assert product.catalog_category_id == category.pk
    assert account.feed_intent_revision == 1
