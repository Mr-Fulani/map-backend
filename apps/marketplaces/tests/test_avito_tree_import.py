import pytest

from apps.marketplaces.avito_tree_import import AvitoTreeImporter, has_tree, load_tree
from apps.products.models import TenantCatalogCategory
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService

# Маленькое дерево-фикстура: категория → подкатегория → виды (лист).
FAKE_TREE = [
    {
        'name': 'Запчасти', 'slug': 'zapchasti', 'children': [
            {'name': 'Для автомобилей', 'slug': 'dlya_avto', 'children': [
                {'name': 'Двигатель', 'slug': 'dvigatel', 'children': [
                    {'name': 'Патрубки вентиляции', 'slug': None, 'children': []},
                    {'name': 'Поршни, шатуны, кольца', 'slug': None, 'children': []},
                ]},
            ]},
        ],
    },
]


def _make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def _auto_parts_domain():
    domain, _ = CatalogDomain.objects.get_or_create(slug='auto_parts', defaults={'name': 'Автозапчасти'})
    return domain


@pytest.mark.django_db
class TestAvitoTreeImporter:
    def test_builds_nested_tree_with_part_types(self):
        _auto_parts_domain()
        tenant = _make_tenant('tree-imp-co')

        AvitoTreeImporter('auto_parts', tree=FAKE_TREE).import_for_tenant(tenant)

        leaf = TenantCatalogCategory.objects.get(tenant=tenant, name='Патрубки вентиляции')
        assert leaf.parent.name == 'Двигатель'
        assert leaf.parent.parent.name == 'Для автомобилей'
        assert leaf.parent.parent.parent.name == 'Запчасти'
        assert leaf.parent.parent.parent.parent is None
        assert leaf.external_source == 'avito'

    def test_idempotent(self):
        _auto_parts_domain()
        tenant = _make_tenant('tree-idem-co')
        importer = AvitoTreeImporter('auto_parts', tree=FAKE_TREE)

        first = importer.import_for_tenant(tenant)
        second = importer.import_for_tenant(tenant)

        assert first == 5  # Запчасти, Для автомобилей, Двигатель, 2 вида
        assert second == 0

    def test_new_nodes_get_curated_aliases(self):
        """Узлы дерева Avito при создании получают курируемые синонимы —
        без них авто-классификация не находит узел по названию товара."""
        _auto_parts_domain()
        tenant = _make_tenant('tree-alias-co')

        AvitoTreeImporter('auto_parts', tree=FAKE_TREE).import_for_tenant(tenant)

        engine = TenantCatalogCategory.objects.get(tenant=tenant, name='Двигатель')
        assert 'Фильтр масляный' in engine.aliases
        pistons = TenantCatalogCategory.objects.get(tenant=tenant, name='Поршни, шатуны, кольца')
        assert 'Поршень' in pistons.aliases

    def test_reimport_heals_external_id_and_aliases(self):
        """Повторный импорт дозаполняет slug и синонимы у ранее созданных записей."""
        _auto_parts_domain()
        tenant = _make_tenant('tree-heal-co')
        importer = AvitoTreeImporter('auto_parts', tree=FAKE_TREE)
        importer.import_for_tenant(tenant)

        engine = TenantCatalogCategory.objects.get(tenant=tenant, name='Двигатель')
        TenantCatalogCategory.objects.filter(pk=engine.pk).update(external_id='', aliases=[])

        importer.import_for_tenant(tenant)

        engine.refresh_from_db()
        assert engine.external_id == 'dvigatel'
        assert 'Фильтр масляный' in engine.aliases

    def test_real_auto_parts_tree_is_baked_in(self):
        assert has_tree('auto_parts')
        tree = load_tree('auto_parts')
        assert len(tree) > 0
        # В дереве есть «Патрубки вентиляции» где-то глубоко.
        flat = []

        def walk(nodes):
            for n in nodes:
                flat.append(n['name'])
                walk(n['children'])

        walk(tree)
        assert 'Двигатель' in flat
        assert 'Патрубки вентиляции' in flat
