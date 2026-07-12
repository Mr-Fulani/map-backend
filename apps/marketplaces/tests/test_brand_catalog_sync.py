from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils.timezone import now

from apps.marketplaces.adapters.avito.brand_catalog import catalog_status, clear_brand_catalog_cache, lookup_brand
from apps.marketplaces.adapters.avito.brand_sync import BrandCatalogSyncError, sync_brand_catalog, validate_catalog
from apps.marketplaces.models import AvitoBrandCatalog


pytestmark = pytest.mark.django_db


def test_sync_saves_verified_catalog_in_database():
    brands = [f'Brand {index}' for index in range(150)]
    with patch('apps.marketplaces.adapters.avito.brand_sync.fetch_avito_brands', return_value=brands):
        state = sync_brand_catalog()

    assert state.brands == brands
    assert catalog_status()['stale'] is False
    clear_brand_catalog_cache()
    assert lookup_brand('Brand 42')['known'] is True


def test_anomalous_shrink_does_not_replace_last_working_catalog():
    old_brands = [f'Old {index}' for index in range(200)]
    AvitoBrandCatalog.objects.create(
        pk=1, source_node='node', field_id=1, brands=old_brands, synced_at=now(),
    )
    with patch(
        'apps.marketplaces.adapters.avito.brand_sync.fetch_avito_brands',
        return_value=[f'New {index}' for index in range(120)],
    ), pytest.raises(BrandCatalogSyncError):
        sync_brand_catalog()

    assert AvitoBrandCatalog.objects.get(pk=1).brands == old_brands


def test_catalog_is_stale_after_three_days():
    AvitoBrandCatalog.objects.create(
        pk=1,
        source_node='node',
        field_id=1,
        brands=[f'Brand {index}' for index in range(150)],
        synced_at=now() - timedelta(days=3, seconds=1),
    )
    assert catalog_status()['stale'] is True


def test_validation_rejects_empty_catalog():
    with pytest.raises(BrandCatalogSyncError):
        validate_catalog([])
