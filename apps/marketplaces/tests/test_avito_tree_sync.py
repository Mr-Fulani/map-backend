from unittest.mock import MagicMock, patch

import pytest

from apps.marketplaces.avito_tree_import import load_tree
from apps.marketplaces.avito_tree_sync import (
    AvitoCategoryTreeSyncService,
    AvitoTreeCallBudget,
    AvitoLiveTreeBuilder,
    AvitoTreeLimitExceeded,
    AvitoTreeSyncError,
    _request_avito_values,
    _validated_avito_api_url,
)
from apps.marketplaces.models import AvitoCategoryTreeSnapshot


def _flat_tree(count: int) -> list[dict]:
    return [
        {'name': f'Категория {index}', 'slug': f'category-{index}', 'children': []}
        for index in range(count)
    ]


@pytest.mark.parametrize(
    'url',
    [
        'http://api.avito.ru/values.json',
        'https://api.avito.ru.evil.test/values.json',
        'https://api.avito.ru@evil.test/values.json',
        'https://evil.test@api.avito.ru/values.json',
        'https://api.avito.ru:444/values.json',
        'https://api.avito.ru/values.json#fragment',
    ],
)
def test_dynamic_values_link_rejects_non_avito_origins(url):
    with pytest.raises(AvitoTreeSyncError, match='недоверенным origin'):
        _validated_avito_api_url(url)


def test_dynamic_values_link_uses_pinned_bounded_transport(settings):
    settings.AVITO_API_RESPONSE_MAX_BYTES = 1234

    with patch(
        'apps.marketplaces.avito_tree_sync.request_public_http_url'
    ) as request_public:
        _request_avito_values(
            'https://api.avito.ru/autoload/values.json?category=engine',
            'secret-token',
        )

    request_public.assert_called_once_with(
        'https://api.avito.ru/autoload/values.json?category=engine',
        timeout=(5, 30),
        headers={
            'Accept': 'application/json',
            'Authorization': 'Bearer secret-token',
        },
        max_response_bytes=1234,
        redirect_policy='none',
    )


def test_dynamic_values_link_does_not_send_token_to_rejected_url():
    with patch(
        'apps.marketplaces.avito_tree_sync.request_public_http_url'
    ) as request_public:
        with pytest.raises(AvitoTreeSyncError):
            _request_avito_values('https://evil.test/steal', 'secret-token')

    request_public.assert_not_called()


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


def _raw_auto_parts_tree(children):
    return [{
        'name': 'Транспорт',
        'nested': [{
            'name': 'Запчасти и аксессуары',
            'slug': 'auto-parts',
            'nested': children,
        }],
    }]


def test_live_builder_rejects_malformed_nested_shape():
    adapter = MagicMock()
    adapter.get_category_tree.return_value = [{
        'name': 'Транспорт',
        'nested': 'not-a-list',
    }]

    with pytest.raises(AvitoTreeSyncError, match='nested'):
        AvitoLiveTreeBuilder(adapter, previous_tree=[]).build_auto_parts()


def test_live_builder_enforces_depth_limit_without_partial_result(settings):
    settings.AVITO_TREE_MAX_DEPTH = 1
    adapter = MagicMock()
    adapter.get_category_tree.return_value = _raw_auto_parts_tree([{
        'name': 'Двигатель',
        'slug': 'engine',
        'nested': [{'name': 'Поршневая', 'slug': 'pistons'}],
    }])

    with pytest.raises(AvitoTreeLimitExceeded, match='глубина'):
        AvitoLiveTreeBuilder(adapter, previous_tree=[]).build_auto_parts()

    adapter.get_node_fields.assert_not_called()


def test_live_builder_enforces_node_limit_before_materializing_values(settings):
    settings.AVITO_TREE_MAX_NODES = 2
    adapter = MagicMock()
    adapter.get_category_tree.return_value = _raw_auto_parts_tree([{
        'name': 'Двигатель',
        'slug': 'engine',
    }])
    adapter.get_node_fields.return_value = {
        'fields': [{
            'tag': 'EngineSparePartType',
            'content': [{
                'values': [
                    {'value': 'Поршни'},
                    {'value': 'Клапаны'},
                ],
            }],
        }],
    }

    with patch('apps.marketplaces.avito_tree_sync.time.sleep'):
        with pytest.raises(AvitoTreeLimitExceeded, match='узлов'):
            AvitoLiveTreeBuilder(adapter, previous_tree=[]).build_auto_parts()


def test_live_builder_enforces_leaf_limit(settings):
    settings.AVITO_TREE_MAX_LEAVES = 1
    adapter = MagicMock()
    adapter.get_category_tree.return_value = _raw_auto_parts_tree([
        {'name': 'Двигатель', 'slug': ''},
        {'name': 'Кузов', 'slug': ''},
    ])

    with pytest.raises(AvitoTreeLimitExceeded, match='листьев'):
        AvitoLiveTreeBuilder(adapter, previous_tree=[]).build_auto_parts()


def test_live_builder_enforces_total_call_budget_before_next_request():
    adapter = MagicMock()
    adapter.get_category_tree.return_value = _raw_auto_parts_tree([{
        'name': 'Двигатель',
        'slug': 'engine',
    }])
    budget = AvitoTreeCallBudget(maximum=1)

    with pytest.raises(AvitoTreeLimitExceeded, match='API-вызовов'):
        AvitoLiveTreeBuilder(
            adapter,
            previous_tree=[],
            call_budget=budget,
        ).build_auto_parts()

    assert budget.used == 1
    adapter.get_node_fields.assert_not_called()


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
