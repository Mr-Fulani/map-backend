from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedRun,
    OzonAccountProfile,
    OzonCategoryAttributeSnapshot,
    OzonCategoryTreeSnapshot,
)
from apps.marketplaces.ozon_catalog import (
    OzonCatalogError,
    catalog_tree_level_from_tree,
    catalog_types_from_tree,
    normalize_category_attributes,
    normalize_category_tree,
)
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


TREE_READ = (
    'apps.marketplaces.ozon_catalog.OzonSellerClient.'
    'get_description_category_tree'
)
ATTRIBUTE_READ = (
    'apps.marketplaces.ozon_catalog.OzonSellerClient.'
    'get_description_category_attributes'
)


def _tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    return tenant, owner_access_token(tenant)


def _account(tenant, client_id: str, *, marketplace: str = 'ozon'):
    credentials = (
        {'client_id': client_id, 'api_key': 'read-only-key'}
        if marketplace == 'ozon'
        else {'client_id': client_id, 'client_secret': 'avito-secret'}
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name=f'{marketplace} {client_id}',
        marketplace=marketplace,
        external_id=client_id,
        credentials_enc=encrypt(credentials),
    )
    if marketplace == 'ozon':
        OzonAccountProfile.objects.create(
            account=account,
            roles=['Description Category'],
            api_methods=[
                '/v1/description-category/tree',
                '/v1/description-category/attribute',
            ],
            last_checked_at=timezone.now(),
        )
    return account


def _tree(type_id: int = 202):
    return [{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [{
            'type_id': type_id,
            'type_name': 'Автозапчасть',
            'disabled': False,
            'children': [],
        }],
    }]


def _refresh(client, token, account, payload):
    return client.post(
        f'/api/v1/accounts/{account.pk}/ozon-catalog/',
        payload,
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


def _browse(client, token, account, params=None):
    return client.get(
        f'/api/v1/accounts/{account.pk}/ozon-catalog/types/',
        params or {},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


def _tree_level(client, token, account, params=None):
    return client.get(
        f'/api/v1/accounts/{account.pk}/ozon-catalog/tree-level/',
        params or {},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


def _snapshot(account, raw_tree, *, revision):
    tree, node_count, active_type_count = normalize_category_tree(raw_tree)
    return OzonCategoryTreeSnapshot.objects.create(
        account=account,
        language=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
        schema_hash=revision * 64,
        tree=tree,
        node_count=node_count,
        active_type_count=active_type_count,
    )


@pytest.fixture
def catalog_setup(settings):
    tenant, token = _tenant('ozon-catalog')
    other, other_token = _tenant('ozon-catalog-other')
    account = _account(tenant, 'catalog-client')
    other_account = _account(other, 'catalog-client-other')
    settings.OZON_ACCOUNT_CONNECTION_ENABLED = True
    settings.OZON_ACCOUNT_CONNECTION_TENANT_SLUGS = tuple(sorted({
        tenant.slug, other.slug,
    }))
    settings.OZON_ACCOUNT_CONNECTION_CLIENT_IDS = tuple(sorted({
        account.external_id, other_account.external_id,
    }))
    return tenant, token, account, other, other_token, other_account


@pytest.mark.django_db
def test_tree_refresh_is_versioned_bounded_and_has_no_avito_feed_side_effect(
    catalog_setup,
):
    _, token, account, *_ = catalog_setup
    client = Client()
    payload = {
        'scope': 'tree',
        'language': 'DEFAULT',
        'confirm_ozon_read_only_access': True,
    }

    with patch(TREE_READ, return_value=_tree()) as provider_read:
        first = _refresh(client, token, account, payload)
        second = _refresh(client, token, account, payload)

    assert first.status_code == second.status_code == 200
    assert provider_read.call_count == 2
    assert OzonCategoryTreeSnapshot.objects.filter(account=account).count() == 1
    body = second.json()
    assert body['tree']['node_count'] == 2
    assert body['tree']['active_type_count'] == 1
    assert len(body['tree']['revision']) == 64
    assert body['attribute_schema_count'] == 0
    assert MarketplaceFeedRun.objects.count() == 0

    local_read = client.get(
        f'/api/v1/accounts/{account.pk}/ozon-catalog/',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    assert local_read.status_code == 200
    assert local_read.json()['tree']['revision'] == body['tree']['revision']


@pytest.mark.django_db
def test_catalog_endpoint_fences_tenant_and_provider_before_external_read(
    catalog_setup,
):
    tenant, token, account, _, other_token, _ = catalog_setup
    avito = _account(tenant, 'avito-client', marketplace='avito')
    payload = {
        'scope': 'tree',
        'confirm_ozon_read_only_access': True,
    }

    with patch(TREE_READ) as provider_read:
        foreign = _refresh(Client(), other_token, account, payload)
        wrong_provider = _refresh(Client(), token, avito, payload)

    assert foreign.status_code == wrong_provider.status_code == 404
    provider_read.assert_not_called()


@pytest.mark.django_db
def test_catalog_refresh_requires_confirmation_and_exact_rollout(catalog_setup, settings):
    _, token, account, *_ = catalog_setup
    client = Client()
    with patch(TREE_READ) as provider_read:
        missing_confirmation = _refresh(client, token, account, {'scope': 'tree'})
        settings.OZON_ACCOUNT_CONNECTION_CLIENT_IDS = ('another-client',)
        blocked = _refresh(client, token, account, {
            'scope': 'tree',
            'confirm_ozon_read_only_access': True,
        })

    assert missing_confirmation.status_code == 400
    assert blocked.status_code == 503
    assert blocked.json()['code'] == 'provider_disabled'
    provider_read.assert_not_called()


@pytest.mark.django_db
def test_attribute_schema_requires_active_leaf_and_tracks_required_fields(
    catalog_setup,
):
    _, token, account, *_ = catalog_setup
    client = Client()
    confirmation = {'confirm_ozon_read_only_access': True}
    with patch(TREE_READ, return_value=_tree()):
        assert _refresh(client, token, account, {
            'scope': 'tree', **confirmation,
        }).status_code == 200
    attributes = [{
        'id': 85,
        'attribute_complex_id': 0,
        'name': 'Бренд',
        'description': 'Марка детали',
        'type': 'String',
        'is_collection': False,
        'is_required': True,
        'is_aspect': False,
        'max_value_count': 1,
        'group_name': 'Основные',
        'group_id': 1,
        'dictionary_id': 42,
        'category_dependent': True,
        'complex_is_collection': False,
    }]
    with patch(ATTRIBUTE_READ, return_value=attributes) as provider_read:
        invalid = _refresh(client, token, account, {
            'scope': 'attributes',
            'description_category_id': 101,
            'type_id': 999,
            **confirmation,
        })
        valid = _refresh(client, token, account, {
            'scope': 'attributes',
            'description_category_id': 101,
            'type_id': 202,
            **confirmation,
        })

    assert invalid.status_code == 400
    assert invalid.json()['code'] == 'invalid_category_type'
    assert valid.status_code == 200
    assert provider_read.call_count == 1
    assert valid.json()['attribute_schema_count'] == 1
    assert valid.json()['latest_attribute_schema']['required_attribute_count'] == 1
    snapshot = OzonCategoryAttributeSnapshot.objects.get(account=account)
    assert snapshot.attributes[0]['dictionary_id'] == 42


@pytest.mark.django_db
def test_catalog_snapshots_are_isolated_for_multiple_accounts(catalog_setup):
    _, token, account, _, other_token, other_account = catalog_setup
    payload = {
        'scope': 'tree',
        'confirm_ozon_read_only_access': True,
    }
    with patch(TREE_READ, side_effect=[_tree(202), _tree(303)]):
        first = _refresh(Client(), token, account, payload)
        second = _refresh(Client(), other_token, other_account, payload)

    assert first.status_code == second.status_code == 200
    assert OzonCategoryTreeSnapshot.objects.filter(account=account).count() == 1
    assert OzonCategoryTreeSnapshot.objects.filter(account=other_account).count() == 1
    assert first.json()['tree']['revision'] != second.json()['tree']['revision']


@pytest.mark.django_db
def test_catalog_type_browser_reads_latest_local_account_snapshot_only(catalog_setup):
    _, token, account, _, _, other_account = catalog_setup
    _snapshot(account, _tree(202), revision='a')
    latest = _snapshot(account, _tree(303), revision='b')
    _snapshot(other_account, _tree(404), revision='c')

    with patch(TREE_READ) as provider_read:
        response = _browse(Client(), token, account)

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['meta']['tree_revision'] == latest.schema_hash
    assert body['meta']['total'] == 1
    assert body['data'] == [{
        'description_category_id': 101,
        'type_id': 303,
        'category_path': 'Автотовары',
        'type_name': 'Автозапчасть',
    }]
    provider_read.assert_not_called()


@pytest.mark.django_db
def test_catalog_type_browser_search_and_pagination_are_bounded(catalog_setup):
    _, token, account, *_ = catalog_setup
    raw_tree = [{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [
            {
                'type_id': 202,
                'type_name': 'Амортизатор',
                'disabled': False,
                'children': [],
            },
            {
                'type_id': 303,
                'type_name': 'Фильтр масляный',
                'disabled': False,
                'children': [],
            },
        ],
    }]
    _snapshot(account, raw_tree, revision='d')

    search = _browse(Client(), token, account, {'search': 'фильтр'})
    page = _browse(Client(), token, account, {'page_size': 1, 'page': 2})
    oversized = _browse(Client(), token, account, {'page_size': 500})
    invalid_search = _browse(Client(), token, account, {'search': 'x' * 121})

    assert search.status_code == page.status_code == oversized.status_code == 200
    assert search.json()['meta']['total'] == 1
    assert search.json()['data'][0]['type_id'] == 303
    assert page.json()['meta']['page_size'] == 1
    assert page.json()['data'][0]['type_id'] == 303
    assert oversized.json()['meta']['page_size'] == 50
    assert invalid_search.status_code == 400


@pytest.mark.django_db
def test_catalog_type_browser_fences_tenant_and_provider_without_provider_io(
    catalog_setup,
):
    tenant, token, account, _, other_token, _ = catalog_setup
    avito = _account(tenant, 'avito-browser', marketplace='avito')
    _snapshot(account, _tree(), revision='e')

    with patch(TREE_READ) as provider_read:
        foreign = _browse(Client(), other_token, account)
        wrong_provider = _browse(Client(), token, avito)

    assert foreign.status_code == wrong_provider.status_code == 404
    provider_read.assert_not_called()


@pytest.mark.django_db
def test_catalog_type_browser_returns_empty_local_state_before_first_snapshot(
    catalog_setup,
):
    _, token, account, *_ = catalog_setup

    with patch(TREE_READ) as provider_read:
        response = _browse(Client(), token, account)

    assert response.status_code == 200
    assert response.json()['data'] == []
    assert response.json()['meta']['total'] == 0
    assert response.json()['meta']['tree_revision'] is None
    provider_read.assert_not_called()


def test_catalog_type_flattening_hides_disabled_branches_and_keeps_full_path():
    tree, _, _ = normalize_category_tree([{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [{
            'description_category_id': 102,
            'category_name': 'Легковые автомобили',
            'disabled': False,
            'children': [{
                'type_id': 202,
                'type_name': 'Амортизатор',
                'disabled': False,
                'children': [],
            }],
        }, {
            'description_category_id': 103,
            'category_name': 'Архивная ветка',
            'disabled': True,
            'children': [{
                'type_id': 303,
                'type_name': 'Скрытый тип',
                'disabled': False,
                'children': [],
            }],
        }],
    }])

    assert catalog_types_from_tree(tree) == [{
        'description_category_id': 102,
        'type_id': 202,
        'category_path': 'Автотовары → Легковые автомобили',
        'type_name': 'Амортизатор',
    }]


def test_catalog_tree_level_navigates_hierarchy_and_hides_dead_branches():
    tree, _, _ = normalize_category_tree([{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [{
            'description_category_id': 102,
            'category_name': 'Автозапчасти',
            'disabled': False,
            'children': [{
                'type_id': 202,
                'type_name': 'Шланг тормозной',
                'disabled': False,
                'children': [],
            }],
        }, {
            'description_category_id': 103,
            'category_name': 'Архив',
            'disabled': True,
            'children': [{
                'type_id': 303,
                'type_name': 'Скрытый тип',
                'disabled': False,
                'children': [],
            }],
        }],
    }])

    root = catalog_tree_level_from_tree(tree)
    nested = catalog_tree_level_from_tree(tree, parent_ids=(101, 102))

    assert root == {
        'path': [],
        'options': [{
            'kind': 'category',
            'description_category_id': 101,
            'type_id': None,
            'name': 'Автотовары',
            'category_path': 'Автотовары',
        }],
    }
    assert nested == {
        'path': [
            {'description_category_id': 101, 'name': 'Автотовары'},
            {'description_category_id': 102, 'name': 'Автозапчасти'},
        ],
        'options': [{
            'kind': 'type',
            'description_category_id': 102,
            'type_id': 202,
            'name': 'Шланг тормозной',
            'category_path': 'Автотовары → Автозапчасти',
        }],
    }


@pytest.mark.django_db
def test_catalog_tree_level_api_is_local_account_scoped_and_read_only(catalog_setup):
    _, token, account, _, other_token, _ = catalog_setup
    _snapshot(account, _tree(202), revision='f')

    with patch(TREE_READ) as provider_read:
        root = _tree_level(Client(), token, account)
        leaf = _tree_level(Client(), token, account, {'parent': '101'})
        foreign = _tree_level(Client(), other_token, account)

    assert root.status_code == leaf.status_code == 200
    assert root.json()['data']['options'][0]['kind'] == 'category'
    assert leaf.json()['data']['options'] == [{
        'kind': 'type',
        'description_category_id': 101,
        'type_id': 202,
        'name': 'Автозапчасть',
        'category_path': 'Автотовары',
        'policy': {
            'enabled_override': None,
            'effective_enabled': True,
            'enabled_source': None,
            'margin_pct': None,
            'effective_margin_pct': '0',
            'margin_source': None,
        },
    }]
    assert foreign.status_code == 404
    provider_read.assert_not_called()


def test_schema_drift_and_resource_limits_fail_closed(settings):
    settings.OZON_CATALOG_MAX_NODES = 1
    with pytest.raises(OzonCatalogError) as tree_error:
        normalize_category_tree(_tree())
    assert tree_error.value.code == 'schema_limit_exceeded'

    settings.OZON_CATALOG_MAX_ATTRIBUTES = 1
    with pytest.raises(OzonCatalogError) as attribute_error:
        normalize_category_attributes([{'id': 1}, {'id': 2}])
    assert attribute_error.value.code == 'schema_limit_exceeded'
