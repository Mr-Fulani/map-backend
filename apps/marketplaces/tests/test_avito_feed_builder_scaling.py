import hashlib
import tempfile
import time
import tracemalloc
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from django.db import connection
from django.db.models import Prefetch
from django.test.utils import CaptureQueriesContext

from apps.marketplaces.adapters.avito import feed_builder
from apps.marketplaces.models import (
    CategoryMapping,
    Listing,
    MarketplacePlacementAddress,
)
from apps.marketplaces.tests.test_avito import (
    make_account,
    make_listing,
    make_product,
    make_tenant,
)
from apps.products.models import Product, ProductImage, TenantCatalogCategory


def _feed_listings(tenant, account, count):
    base_product = make_product(tenant)
    listings = []
    for index in range(count):
        product = Product.objects.create(
            tenant=tenant,
            datasource=base_product.datasource,
            article=f'SCALE-{index}',
            name=f'Масштабируемый товар {index}',
            brand='Bosch',
            price='3500',
            stock_qty=5,
            category_1c='Тормоза',
            condition='new',
        )
        ProductImage.objects.create(
            product=product,
            s3_key=f'products/feed/scale-{index}.jpg',
            sha256=f'scale-{index}',
            status=ProductImage.Status.IMPORTED,
        )
        listings.append(make_listing(tenant, product, account))
    return listings


@pytest.mark.django_db
def test_build_feed_relation_queries_are_bounded_per_batch():
    tenant = make_tenant('feed-builder-bounded-queries')
    account = make_account(tenant)
    MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=account,
        name='Основной склад',
        address='Москва, Складская, 1',
        is_default=True,
    )
    CategoryMapping.objects.create(
        tenant=tenant,
        marketplace=CategoryMapping.MARKETPLACE_AVITO,
        category_source='Тормоза',
        category_target='Запчасти и аксессуары',
        category_id=1,
        attributes_map={'GoodsType': 'Запчасти'},
    )
    created = _feed_listings(tenant, account, 24)
    listings = list(
        Listing.objects.filter(pk__in=[listing.pk for listing in created])
        .select_related('tenant', 'product', 'account')
        .order_by('pk')
    )

    with CaptureQueriesContext(connection) as queries:
        payload = feed_builder.build_feed(listings)

    assert len(queries) <= 6
    assert len(ET.fromstring(payload).findall('Ad')) == 24


@pytest.mark.django_db
def test_build_feed_reuses_caller_prefetches_without_database_queries():
    tenant = make_tenant('feed-builder-prefetch')
    account = make_account(tenant)
    MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=account,
        name='Основной склад',
        address='Казань, Складская, 2',
        is_default=True,
    )
    CategoryMapping.objects.create(
        tenant=tenant,
        marketplace=CategoryMapping.MARKETPLACE_AVITO,
        category_source='Тормоза',
        category_target='Запчасти и аксессуары',
        category_id=1,
        attributes_map={'GoodsType': 'Запчасти'},
    )
    created = _feed_listings(tenant, account, 4)
    listings = list(
        Listing.objects.filter(pk__in=[listing.pk for listing in created])
        .select_related('tenant', 'product', 'account', 'product__catalog_category')
        .prefetch_related(
            'tenant__category_mappings',
            'account__placement_addresses',
            Prefetch(
                'product__images',
                queryset=ProductImage.objects.prefetch_related('variants'),
            ),
        )
        .order_by('pk')
    )

    with CaptureQueriesContext(connection) as queries:
        payload = feed_builder.build_feed(listings)

    assert len(queries) == 0
    assert len(ET.fromstring(payload).findall('Ad')) == 4


@pytest.mark.django_db
def test_build_feed_rolls_large_intermediate_xml_to_disk():
    tenant = make_tenant('feed-builder-spool')
    account = make_account(tenant)
    product = make_product(tenant)
    listing = make_listing(tenant, product, account)
    listing.description_ai = 'д' * 7500
    created_files = []
    real_spooled_file = tempfile.SpooledTemporaryFile

    def tracked_spooled_file(*args, **kwargs):
        spooled_file = real_spooled_file(*args, **kwargs)
        created_files.append(spooled_file)
        return spooled_file

    with patch.object(
        feed_builder.tempfile,
        'SpooledTemporaryFile',
        side_effect=tracked_spooled_file,
    ):
        payload = feed_builder.build_feed([listing] * 150)

    assert created_files[0]._rolled is True
    assert isinstance(payload, bytes)
    assert len(ET.fromstring(payload).findall('Ad')) == 150


@pytest.mark.django_db
def test_build_feed_keeps_category_ancestry_with_select_related_leaf():
    """The optimized ORM path must resolve the same deep Avito taxonomy."""

    tenant = make_tenant('feed-builder-category-ancestry')
    account = make_account(tenant)
    product = make_product(tenant)
    leaf = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Трансмиссия и привод',
        normalized_name='трансмиссияипривод',
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
        external_source='avito',
        external_id='transmissiia_i_privod',
    )
    subtype = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Крепёж КПП',
        normalized_name='крепёжкпп',
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
        external_source='avito',
        parent=leaf,
    )
    product.catalog_category = subtype
    product.save(update_fields=['catalog_category'])
    listing = make_listing(tenant, product, account)

    uncached_leaf = list(
        Listing.objects.filter(pk=listing.pk)
        .select_related('tenant', 'product', 'account')
    )
    cached_leaf = list(
        Listing.objects.filter(pk=listing.pk)
        .select_related(
            'tenant', 'product', 'account', 'product__catalog_category',
        )
    )

    assert feed_builder.build_feed(cached_leaf) == feed_builder.build_feed(uncached_leaf)


@pytest.mark.django_db
def test_missing_oem_fallback_rebuild_is_byte_deterministic():
    """A lost temp file must be reproducible for the same durable listing."""

    tenant = make_tenant('feed-builder-deterministic-oem')
    account = make_account(tenant)
    product = make_product(tenant)
    product.article = ''
    product.oem_numbers = []
    product.save(update_fields=['article', 'oem_numbers'])
    listing = make_listing(tenant, product, account)

    def reload_projection():
        return list(
            Listing.objects.filter(pk=listing.pk)
            .select_related('tenant', 'product', 'account')
            .order_by('created_at', 'pk')
        )

    # Model a worker restart: the second build must not rely on process-local
    # objects or relation caches retained by the first build.
    first = feed_builder.build_feed(reload_projection())
    second = feed_builder.build_feed(reload_projection())
    expected_suffix = hashlib.sha256(
        f'avito-oem-fallback:v1:{listing.pk}'.encode('ascii'),
    ).hexdigest()[:10].upper()

    assert first == second
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
    assert f'<OEM>NA{expected_suffix}</OEM>'.encode() in first


@pytest.mark.django_db
def test_private_stream_writer_handles_ten_thousand_ads_with_bounded_memory():
    """P6 acceptance gate: exact 10k output stays disk-backed and bounded."""

    tenant = make_tenant('feed-builder-ten-thousand')
    account = make_account(tenant)
    product = make_product(tenant)
    listing = make_listing(tenant, product, account)

    tracemalloc.start()
    started_at = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(mode='w+b') as payload_file:
            result = feed_builder.write_feed(
                (listing for _ in range(10_000)),
                payload_file,
                max_bytes=268_435_456,
            )
            elapsed_seconds = time.monotonic() - started_at
            _, peak_bytes = tracemalloc.get_traced_memory()
            payload_file.flush()
            assert payload_file.tell() == result.size_bytes
            payload_file.seek(0)
            assert payload_file.read(64).startswith(b'<?xml')
            payload_file.seek(-6, 2)
            assert payload_file.read() == b'</Ads>'
    finally:
        tracemalloc.stop()

    assert result.listing_count == 10_000
    assert 1_000_000 < result.size_bytes < 268_435_456
    assert peak_bytes < 96 * 1024 * 1024
    assert elapsed_seconds < 120
