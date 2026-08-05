from types import SimpleNamespace

from apps.image_search.services.pipeline import _final_outcome, build_cache_key
from apps.products.models import ProductImage


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
