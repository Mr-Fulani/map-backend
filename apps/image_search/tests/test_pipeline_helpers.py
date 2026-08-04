from apps.image_search.services.pipeline import build_cache_key


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
