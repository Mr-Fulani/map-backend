from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.image_search.services.pipeline import _final_outcome, build_cache_key, run_for_product
from apps.image_search.sources.base import ImageCandidate
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService


class ProductStub:
    pk = 10
    tenant_id = 20
    article = ''
    brand = ''
    catalog_category_id = None


def test_cache_key_is_tenant_and_product_scoped_for_blank_identity():
    first = ProductStub()
    second = ProductStub()
    second.pk = 11
    assert build_cache_key(first) != build_cache_key(second)


def test_cache_key_changes_with_tenant():
    first = ProductStub()
    second = ProductStub()
    second.tenant_id = 21
    assert build_cache_key(first) != build_cache_key(second)


def test_outcome_explains_relevance_rejections_instead_of_rate_limit():
    result = _final_outcome(
        saved=[],
        found_count=30,
        rejected_count=30,
        eligible_count=0,
        download_failed_count=0,
        attempted_sources=['brave', 'tavily'],
        errors=[],
    )

    assert result['reason_code'] == 'rejected_by_relevance'
    assert '30' in result['message']
    assert 'огранич' not in result['message'].lower()


def test_outcome_reports_low_quality_images_as_processing_candidates():
    result = _final_outcome(
        saved=[SimpleNamespace(pk=1, status=ProductImage.Status.LOW_CONFIDENCE)],
        found_count=1,
        rejected_count=0,
        eligible_count=1,
        download_failed_count=0,
        attempted_sources=['brave'],
        errors=[],
    )

    assert result['reason_code'] == 'found'
    assert result['saved_count'] == 1
    assert 'улучшения качества: 1' in result['message']


@pytest.mark.django_db
def test_web_search_creates_candidates_with_review_status():
    tenant, _ = TenantService.create_tenant(
        'Image Review', 'image-review-status', 'image-review@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='Колодки тормозные BREMBO P50136',
        price='1000.00',
    )
    query = 'BREMBO "P50136" автозапчасть'
    candidate = ImageCandidate(
        url='https://images.example.com/brembo-P50136.jpg',
        source_id='brave',
        tier=3,
        width=1200,
        height=900,
        raw_meta={'query': query, 'confidence': 'HIGH', 'title': 'BREMBO P50136'},
    )
    source = MagicMock(
        source_id='brave',
        max_queries=1,
        last_error='',
        last_error_code='',
    )
    source.build_queries.return_value = [(query, 'HIGH')]
    source.search.return_value = [candidate]
    review_image = ProductImage.objects.create(
        product=product,
        s3_key='products/test/brembo-P50136.jpg',
        s3_key_thumb='products/test/brembo-P50136_thumb.jpg',
        url_source=candidate.url,
        sha256='a' * 64,
        status=ProductImage.Status.NEEDS_REVIEW,
    )
    uploader = MagicMock()
    uploader.process.return_value = review_image

    with patch(
        'apps.image_search.services.pipeline.get_active_sources', return_value=[source],
    ), patch(
        'apps.image_search.services.pipeline.PhotoUploadPipeline', return_value=uploader,
    ), patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(True, ['trusted_code_match'], 0.95),
    ), patch(
        'apps.image_search.services.pipeline.score', return_value=0.82,
    ), patch('apps.image_search.services.pipeline._record_candidate_assessments'):
        result = run_for_product(product)

    assert result['saved_count'] == 1
    uploader.process.assert_called_once_with(
        candidate.url,
        product,
        source_id='brave',
        status=ProductImage.Status.NEEDS_REVIEW,
        validate_quality=True,
        allow_low_resolution=True,
    )


@pytest.mark.django_db
def test_web_search_imported_backfill_only_restores_search_sources():
    tenant, _ = TenantService.create_tenant(
        'Image Backfill', 'image-backfill-status', 'image-backfill@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        name='BREMBO P50136',
        price='1000.00',
    )
    brave_image = ProductImage.objects.create(
        product=product,
        s3_key='products/test/brave.jpg',
        sha256='b' * 64,
        source_id='brave',
        status=ProductImage.Status.IMPORTED,
    )
    imported_image = ProductImage.objects.create(
        product=product,
        s3_key='products/test/1c.jpg',
        sha256='c' * 64,
        source_id='1c',
        status=ProductImage.Status.IMPORTED,
    )

    migration = import_module(
        'apps.products.migrations.0031_restore_web_search_images_for_review',
    )
    from django.apps import apps
    migration.restore_web_search_images_for_review(apps, None)

    brave_image.refresh_from_db()
    imported_image.refresh_from_db()
    assert brave_image.status == ProductImage.Status.NEEDS_REVIEW
    assert imported_image.status == ProductImage.Status.IMPORTED
