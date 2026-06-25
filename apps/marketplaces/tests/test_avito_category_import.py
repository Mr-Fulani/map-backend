import pytest

from apps.marketplaces.avito_category_import import AvitoCatalogImporter, load_avito_leaves
from apps.marketplaces.models import CategoryMapping
from apps.products.models import TenantCatalogCategory
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService

# Небольшое дерево-фикстура: два листа под разными ветками.
FAKE_LEAVES = [
    {
        'name': 'Моторные масла',
        'slug': 'motornye_masla',
        'path': ['Запчасти и аксессуары', 'Масла и автохимия', 'Моторные масла'],
        'required': ['SAE', 'Volume'],
        'fixed': {'Category': 'Запчасти и аксессуары', 'GoodsType': 'Масла и автохимия',
                  'ProductType': 'Моторные масла'},
    },
    {
        'name': 'Двигатель',
        'slug': 'dvigatel',
        'path': ['Запчасти и аксессуары', 'Запчасти', 'Для автомобилей', 'Двигатель'],
        'required': ['SparePartType'],
        'fixed': {'Category': 'Запчасти и аксессуары', 'GoodsType': 'Запчасти'},
    },
]


def _make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def _auto_parts_domain():
    domain, _ = CatalogDomain.objects.get_or_create(
        slug='auto_parts', defaults={'name': 'Автозапчасти'},
    )
    return domain


@pytest.mark.django_db
class TestAvitoCatalogImporter:
    def test_builds_category_hierarchy_from_path(self):
        _auto_parts_domain()
        tenant = _make_tenant('imp-tree-co')

        AvitoCatalogImporter(leaves=FAKE_LEAVES).import_for_tenant(tenant)

        # Лист «Моторные масла» лежит под «Масла и автохимия» (корень — без родителя).
        leaf = TenantCatalogCategory.objects.get(tenant=tenant, name='Моторные масла')
        assert leaf.parent.name == 'Масла и автохимия'
        assert leaf.parent.parent is None
        assert leaf.external_source == 'avito'
        assert leaf.external_id == 'motornye_masla'
        assert leaf.domain == 'auto_parts'

        # Трёхуровневая ветка «Запчасти → Для автомобилей → Двигатель».
        dv = TenantCatalogCategory.objects.get(tenant=tenant, name='Двигатель')
        assert dv.parent.name == 'Для автомобилей'
        assert dv.parent.parent.name == 'Запчасти'

    def test_creates_mappings_with_attributes(self):
        _auto_parts_domain()
        tenant = _make_tenant('imp-map-co')

        AvitoCatalogImporter(leaves=FAKE_LEAVES).import_for_tenant(tenant)

        m = CategoryMapping.objects.get(tenant=tenant, category_source='Моторные масла')
        assert m.marketplace == CategoryMapping.MARKETPLACE_AVITO
        assert m.category_target == 'Запчасти и аксессуары'
        assert m.attributes_map['GoodsType'] == 'Масла и автохимия'
        assert m.attributes_map['ProductType'] == 'Моторные масла'

    def test_import_is_idempotent(self):
        _auto_parts_domain()
        tenant = _make_tenant('imp-idem-co')
        importer = AvitoCatalogImporter(leaves=FAKE_LEAVES)

        first = importer.import_for_tenant(tenant)
        second = importer.import_for_tenant(tenant)

        assert first['mappings'] == 2
        assert second['categories'] == 0
        assert second['mappings'] == 0

    def test_real_specs_import_full_avito_tree(self):
        _auto_parts_domain()
        tenant = _make_tenant('imp-real-co')
        leaves = load_avito_leaves()

        result = AvitoCatalogImporter(leaves=leaves).import_for_tenant(tenant)

        # Маппинг на каждое уникальное имя листа (одноимённые листья из разных
        # веток ключуются по имени и схлопываются — резолв фида тоже идёт по имени).
        unique_names = {leaf['name'] for leaf in leaves}
        assert result['mappings'] == len(unique_names)
        assert CategoryMapping.objects.filter(tenant=tenant).count() == len(unique_names)
        assert result['mappings'] > 150  # дерево Avito действительно большое
