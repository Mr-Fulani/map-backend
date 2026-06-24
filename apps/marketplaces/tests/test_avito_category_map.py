"""Тесты маппинга внутренней таксономии на категории Avito (без БД, pytest-стиль)."""
from apps.marketplaces.adapters.avito.category_map import (
    LEAF_TO_AVITO,
    ROOT_TO_AVITO,
    _specs,
    avito_spec_for,
    resolve_avito_slug,
)
from apps.products.part_category_seed import BASE_PART_CATEGORY_TREE


class TestAvitoCategoryMap:
    """Проверяет, что вся таксономия сопоставлена с валидными листьями Avito."""

    def test_all_target_slugs_exist_in_specs(self):
        """Каждый целевой slug маппинга присутствует в справочнике Avito."""
        specs = _specs()
        for slug in list(ROOT_TO_AVITO.values()) + list(LEAF_TO_AVITO.values()):
            assert slug in specs, f'slug {slug} отсутствует в avito_field_specs.json'

    def test_every_taxonomy_category_resolves_with_goodstype(self):
        """Каждая наша категория (корень и лист) резолвится в лист Avito с GoodsType."""
        for root in BASE_PART_CATEGORY_TREE:
            pairs = [(root['name'], '')] + [(child[0], root['name']) for child in root.get('children', [])]
            for name, parent in pairs:
                spec = avito_spec_for(name, parent)
                assert spec, f'категория «{name}» не сопоставлена с Avito'
                assert 'GoodsType' in spec['fixed'], f'у «{name}» ({spec["slug"]}) нет GoodsType'

    def test_leaf_override_wins_over_root(self):
        """Лист-override имеет приоритет над значением по умолчанию для корня."""
        # «Фары» относятся к корню «Кузов…» (kuzov), но должны уходить в «Автосвет».
        assert resolve_avito_slug('Фары', 'Кузов, стекла и оптика') == 'avtosvet'

    def test_special_branches_goodstype(self):
        """Шины и масла уходят в свои GoodsType, а не «Запчасти»."""
        assert avito_spec_for('Шины', 'Колеса и шины')['fixed']['GoodsType'] == 'Шины, диски и колёса'
        assert (
            avito_spec_for('Моторные масла', 'Масла, жидкости и автохимия')['fixed']['GoodsType']
            == 'Масла и автохимия'
        )
