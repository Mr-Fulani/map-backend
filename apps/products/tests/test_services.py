import io
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.products.models import Product, ProductImage
from apps.products.services import ProductService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_datasource(tenant):
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name='Test Source',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://example.com', 'user': 'u', 'password': 'p'}),
    )


SAMPLE_DATA = {
    'uuid': None,
    'article': 'A100',
    'name': 'Деталь тестовая',
    'brand': 'BrandX',
    'price': '1500.00',
    'stock_qty': 10,
    'category': 'Кузов',
    'condition': 'new',
}


@pytest.mark.django_db
class TestProductService:
    def test_upsert_creates_new_product(self):
        tenant = make_tenant('create-co')
        ds = make_datasource(tenant)
        product, status = ProductService.upsert_from_source(tenant, ds, SAMPLE_DATA)
        assert status == 'created'
        assert product.article == 'A100'
        assert product.price == Decimal('1500.00')
        assert product.stock_qty == 10

    def test_upsert_unchanged_product_not_updated(self):
        tenant = make_tenant('unchanged-co')
        ds = make_datasource(tenant)
        ProductService.upsert_from_source(tenant, ds, SAMPLE_DATA)
        _, status2 = ProductService.upsert_from_source(tenant, ds, SAMPLE_DATA)
        assert status2 == 'unchanged'

    def test_upsert_detects_price_change(self):
        tenant = make_tenant('price-co')
        ds = make_datasource(tenant)
        ProductService.upsert_from_source(tenant, ds, SAMPLE_DATA)
        updated_data = {**SAMPLE_DATA, 'price': '2000.00'}
        _, status = ProductService.upsert_from_source(tenant, ds, updated_data)
        assert status == 'updated'

    def test_detect_change_type_price_only(self):
        old = {**SAMPLE_DATA}
        new = {**SAMPLE_DATA, 'price': '9999.00'}
        assert ProductService.detect_change_type(old, new) == 'price_only'

    def test_detect_change_type_stock_only(self):
        old = {**SAMPLE_DATA}
        new = {**SAMPLE_DATA, 'stock_qty': 999}
        assert ProductService.detect_change_type(old, new) == 'stock_only'

    def test_detect_change_type_content(self):
        old = {**SAMPLE_DATA}
        new = {**SAMPLE_DATA, 'name': 'Новое название'}
        assert ProductService.detect_change_type(old, new) == 'content'

    def test_upsert_100_products_performance(self):
        """100 товаров создаются без ошибок."""
        tenant = make_tenant('bulk-co')
        ds = make_datasource(tenant)
        for i in range(100):
            data = {**SAMPLE_DATA, 'article': f'ART{i:04d}', 'uuid': None}
            ProductService.upsert_from_source(tenant, ds, data)
        assert Product.objects.filter(tenant=tenant).count() == 100


@pytest.mark.django_db
class TestPhotoUploadPipeline:
    def _make_jpeg_bytes(self, size=(100, 100)) -> bytes:
        from PIL import Image
        img = Image.new('RGB', size, color=(128, 64, 32))
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        return buf.getvalue()

    def test_photo_deduplication_by_sha256(self):
        from apps.products.storage import PhotoUploadPipeline

        tenant = make_tenant('photo-co')
        ds = make_datasource(tenant)
        product, _ = ProductService.upsert_from_source(tenant, ds, SAMPLE_DATA)

        jpeg = self._make_jpeg_bytes()
        mock_storage = MagicMock()
        mock_storage.save = MagicMock(return_value='products/test/1.jpg')

        with patch('apps.products.storage.requests.get') as mock_get:
            mock_get.return_value.content = jpeg
            mock_get.return_value.raise_for_status = MagicMock()
            pipeline = PhotoUploadPipeline(storage=mock_storage)

            img1 = pipeline.process('http://example.com/photo.jpg', product)
            img2 = pipeline.process('http://example.com/photo.jpg', product)

        assert img1 is not None
        assert img1.pk == img2.pk
        assert ProductImage.objects.filter(product=product).count() == 1

    def test_photo_limit_10_per_product(self):
        from apps.products.storage import PhotoUploadPipeline

        tenant = make_tenant('limit-co')
        ds = make_datasource(tenant)
        product, _ = ProductService.upsert_from_source(tenant, ds, SAMPLE_DATA)

        mock_storage = MagicMock()
        pipeline = PhotoUploadPipeline(storage=mock_storage)

        for i in range(10):
            ProductImage.objects.create(
                product=product, s3_key=f'key{i}', sha256=f'{i:064d}', position=i
            )

        with patch('apps.products.storage.requests.get') as mock_get:
            mock_get.return_value.content = self._make_jpeg_bytes()
            mock_get.return_value.raise_for_status = MagicMock()
            result = pipeline.process('http://example.com/extra.jpg', product)

        assert result is None
        assert ProductImage.objects.filter(product=product).count() == 10
