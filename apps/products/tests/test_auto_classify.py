"""Тесты авто-классификации товаров (classify_tenant_products)."""
import pytest

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.products.models import ProductCatalogClassification
from apps.products.services import ProductService
from apps.products.tasks import classify_tenant_products
from apps.tenants.services import TenantService


def _tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def _datasource(tenant):
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name='Test Source',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://example.com', 'user': 'u', 'password': 'p'}),
    )


_SAMPLE = {
    'uuid': None,
    'article': 'A100',
    'name': 'Тормозные колодки передние',
    'brand': 'BrandX',
    'price': '1500.00',
    'stock_qty': 10,
    'category': 'Тормозная система',
    'condition': 'new',
}


@pytest.mark.django_db
class TestClassifyTenantProducts:
    """Проверяет авто-классификацию незаполненных товаров и идемпотентность."""

    def test_classifies_products_without_classification(self):
        """Товар без ProductCatalogClassification получает её после запуска таска."""
        tenant = _tenant('auto-classify-co')
        ds = _datasource(tenant)
        product, _, _ = ProductService.upsert_from_source(tenant, ds, _SAMPLE)
        assert not ProductCatalogClassification.objects.filter(product=product).exists()

        result = classify_tenant_products(tenant.id)

        assert ProductCatalogClassification.objects.filter(product=product).exists()
        assert result['classified'] == 1

    def test_idempotent_skips_already_classified(self):
        """Повторный запуск не трогает уже классифицированные товары."""
        tenant = _tenant('auto-classify-skip-co')
        ds = _datasource(tenant)
        ProductService.upsert_from_source(tenant, ds, _SAMPLE)
        classify_tenant_products(tenant.id)

        result = classify_tenant_products(tenant.id)

        assert result['classified'] == 0
