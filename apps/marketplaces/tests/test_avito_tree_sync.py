from unittest.mock import MagicMock, patch

import pytest

from apps.marketplaces.avito_tree_import import load_tree
from apps.marketplaces.avito_tree_sync import (
    AvitoCategoryTreeSyncService,
    AvitoLiveTreeBuilder,
    AvitoTreeSyncError,
)
from apps.marketplaces.models import AvitoCategoryTreeSnapshot


def _flat_tree(count: int) -> list[dict]:
    return [
        {'name': f'Категория {index}', 'slug': f'category-{index}', 'children': []}
        for index in range(count)
    ]


def test_live_builder_uses_exact_transport_root_and_adds_part_types():
    """Одинаковые названия в других разделах не смешиваются с автозапчастями."""
    adapter = MagicMock()
    adapter.get_category_tree.return_value = [
        {
            'name': 'Для дома и дачи',
            'nested': [{'name': 'Запчасти и аксессуары', 'slug': 'home-parts'}],
        },
        {
            'name': 'Транспорт',
            'nested': [{
                'name': 'Запчасти и аксессуары',
                'slug': 'auto-parts',
                'nested': [{'name': 'Двигатель', 'slug': 'engine'}],
            }],
        },
    ]
    adapter.get_node_fields.return_value = {
        'fields': [{
            'tag': 'EngineSparePartType',
            'content': [{
                'values': [
                    {'value': 'Поршни'},
                    {'value': 'Двигатель'},
                ],
            }],
        }],
    }

    with patch('apps.marketplaces.avito_tree_sync.time.sleep'):
        result = AvitoLiveTreeBuilder(adapter, previous_tree=[]).build_auto_parts()

    assert result.tree == [{
        'name': 'Двигатель',
        'slug': 'engine',
        'children': [{'name': 'Поршни', 'slug': None, 'children': []}],
    }]
    adapter.get_node_fields.assert_called_once_with('engine')


def test_live_builder_keeps_previous_deep_values_on_temporary_error():
    """Сбой одного endpoint полей не обрезает уже проверенную ветку."""
    adapter = MagicMock()
    adapter.get_category_tree.return_value = [{
        'name': 'Транспорт',
        'nested': [{
            'name': 'Запчасти и аксессуары',
            'nested': [{'name': 'Двигатель', 'slug': 'engine'}],
        }],
    }]
    adapter.get_node_fields.side_effect = ConnectionError('temporary')
    previous = [{
        'name': 'Двигатель',
        'slug': 'engine',
        'children': [{'name': 'Поршни', 'slug': None, 'children': []}],
    }]

    with patch('apps.marketplaces.avito_tree_sync.time.sleep'):
        result = AvitoLiveTreeBuilder(adapter, previous).build_auto_parts()

    assert result.tree == previous
    assert len(result.warnings) == 1


def test_validation_rejects_suspiciously_short_tree():
    """Короткий или обрезанный ответ Avito не применяется автоматически."""
    with pytest.raises(AvitoTreeSyncError, match='короткое дерево'):
        AvitoCategoryTreeSyncService._validate(_flat_tree(150), _flat_tree(20))


@pytest.mark.django_db
def test_ready_snapshot_has_priority_over_baked_tree():
    """Новые тенанты получают последний проверенный API-снимок."""
    live_tree = _flat_tree(3)
    AvitoCategoryTreeSnapshot.objects.create(
        domain_slug='auto_parts',
        root_name='Запчасти и аксессуары',
        tree=live_tree,
        checksum='test',
        status=AvitoCategoryTreeSnapshot.STATUS_READY,
        node_count=3,
    )

    assert load_tree('auto_parts') == live_tree
