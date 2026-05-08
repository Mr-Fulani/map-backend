import pytest

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import CategoryMapping
from apps.marketplaces.services import CategoryMappingService
from apps.products.models import Product
from apps.products.services import ProductService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_datasource(tenant):
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name='Source',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )


def make_product(tenant, ds, article, category):
    data = {
        'uuid': None, 'article': article, 'name': 'Test', 'brand': 'B',
        'price': '100', 'stock_qty': 1, 'category': category, 'condition': 'new',
    }
    product, _ = ProductService.upsert_from_source(tenant, ds, data)
    return product


@pytest.mark.django_db
class TestCategoryMappingService:
    def test_unmapped_categories_returned_for_new_tenant(self):
        """Новый тенант видит все категории из продуктов как незамапленные."""
        tenant = make_tenant('unmap-co')
        ds = make_datasource(tenant)
        make_product(tenant, ds, 'A1', 'Кузов')
        make_product(tenant, ds, 'A2', 'Двигатель')
        make_product(tenant, ds, 'A3', 'Кузов')  # дубль категории

        unmapped = CategoryMappingService.get_unmapped_categories(tenant)
        assert set(unmapped) == {'Кузов', 'Двигатель'}

    def test_mapped_category_excluded_from_unmapped(self):
        """После создания маппинга категория уходит из unmapped."""
        tenant = make_tenant('mapped-co')
        ds = make_datasource(tenant)
        make_product(tenant, ds, 'B1', 'Тормоза')
        make_product(tenant, ds, 'B2', 'Кузов')

        CategoryMappingService.bulk_create_from_dict(tenant, {
            'Тормоза': {'category_target': 'Запчасти', 'category_id': 9001, 'attributes_map': {}},
        })

        unmapped = CategoryMappingService.get_unmapped_categories(tenant)
        assert 'Тормоза' not in unmapped
        assert 'Кузов' in unmapped

    def test_get_or_suggest_returns_existing_mapping(self):
        tenant = make_tenant('suggest-co')
        CategoryMapping.objects.create(
            tenant=tenant,
            marketplace='avito',
            category_source='Кузов',
            category_target='Кузовные детали',
            category_id=1234,
        )
        mapping = CategoryMappingService.get_or_suggest(tenant, 'Кузов')
        assert mapping is not None
        assert mapping.category_id == 1234

    def test_get_or_suggest_returns_none_for_unknown(self):
        tenant = make_tenant('unknown-co')
        result = CategoryMappingService.get_or_suggest(tenant, 'Несуществующая')
        assert result is None

    def test_bulk_create_from_dict(self):
        tenant = make_tenant('bulk-co')
        mappings = {
            'Кузов': {'category_target': 'Кузовные', 'category_id': 100},
            'Двигатель': {'category_target': 'Двигатели', 'category_id': 200},
        }
        result = CategoryMappingService.bulk_create_from_dict(tenant, mappings)
        assert len(result) == 2
        assert CategoryMapping.objects.filter(tenant=tenant).count() == 2

    def test_bulk_create_is_idempotent(self):
        """Повторный вызов обновляет, не создаёт дубли."""
        tenant = make_tenant('idem-co')
        data = {'Кузов': {'category_target': 'Кузовные', 'category_id': 100}}
        CategoryMappingService.bulk_create_from_dict(tenant, data)
        CategoryMappingService.bulk_create_from_dict(tenant, data)
        assert CategoryMapping.objects.filter(tenant=tenant).count() == 1

    def test_empty_category_products_ignored(self):
        """Товары без категории не попадают в unmapped."""
        tenant = make_tenant('nocat-co')
        ds = make_datasource(tenant)
        Product.objects.create(
            tenant=tenant, datasource=ds, article='X1',
            name='Без категории', price='50', stock_qty=1, category_1c='',
        )
        unmapped = CategoryMappingService.get_unmapped_categories(tenant)
        assert unmapped == []

    def test_tenant_isolation(self):
        """Маппинги одного тенанта не видны другому."""
        t1 = make_tenant('iso1-co')
        t2 = make_tenant('iso2-co')
        ds1 = make_datasource(t1)
        make_product(t1, ds1, 'C1', 'Кузов')
        CategoryMappingService.bulk_create_from_dict(t1, {
            'Кузов': {'category_target': 'K', 'category_id': 1},
        })
        unmapped_t2 = CategoryMappingService.get_unmapped_categories(t2)
        assert unmapped_t2 == []
