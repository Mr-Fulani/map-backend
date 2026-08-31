from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedRun,
    OzonCategoryPolicy,
    OzonCategoryTreeSnapshot,
)
from apps.marketplaces.ozon_catalog import normalize_category_tree
from apps.marketplaces.ozon_category_policies import resolved_category_type_policy
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


TREE_READ = (
    'apps.marketplaces.ozon_catalog.OzonSellerClient.'
    'get_description_category_tree'
)


def _tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant, owner_access_token(tenant)


def _account(tenant, client_id: str, *, marketplace: str = 'ozon'):
    credentials = (
        {'client_id': client_id, 'api_key': 'read-only-key'}
        if marketplace == MarketplaceAccount.MARKETPLACE_OZON
        else {'client_id': client_id, 'client_secret': 'avito-secret'}
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name=f'{marketplace} {client_id}',
        marketplace=marketplace,
        external_id=client_id,
        credentials_enc=encrypt(credentials),
    )


def _tree():
    return [{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [{
            'description_category_id': 102,
            'category_name': 'Автозапчасти',
            'disabled': False,
            'children': [
                {
                    'type_id': 202,
                    'type_name': 'Шланг тормозной',
                    'disabled': False,
                    'children': [],
                },
                {
                    'type_id': 203,
                    'type_name': 'Колодки тормозные',
                    'disabled': False,
                    'children': [],
                },
            ],
        }],
    }]


def _snapshot(account, *, revision: str = 'f'):
    tree, node_count, active_type_count = normalize_category_tree(_tree())
    return OzonCategoryTreeSnapshot.objects.create(
        account=account,
        language=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
        schema_hash=revision * 64,
        tree=tree,
        node_count=node_count,
        active_type_count=active_type_count,
    )


def _tree_level(client, token, account, parent: str = ''):
    params = {'parent': parent} if parent else {}
    return client.get(
        f'/api/v1/accounts/{account.pk}/ozon-catalog/tree-level/',
        params,
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


def _patch_policy(client, token, account, payload):
    return client.patch(
        f'/api/v1/accounts/{account.pk}/ozon-catalog/category-policy/',
        payload,
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


@pytest.fixture
def policy_setup():
    tenant, token = _tenant('ozon-policy')
    other_tenant, other_token = _tenant('ozon-policy-other')
    account = _account(tenant, 'policy-account')
    second_account = _account(tenant, 'policy-account-second')
    other_account = _account(other_tenant, 'policy-account-other')
    snapshot = _snapshot(account)
    _snapshot(second_account)
    _snapshot(other_account)
    return {
        'tenant': tenant,
        'token': token,
        'account': account,
        'second_account': second_account,
        'other_tenant': other_tenant,
        'other_token': other_token,
        'other_account': other_account,
        'snapshot': snapshot,
    }


@pytest.mark.django_db
def test_tree_level_defaults_are_local_and_do_not_create_policy(policy_setup):
    response = _tree_level(
        Client(),
        policy_setup['token'],
        policy_setup['account'],
    )

    assert response.status_code == 200
    option = response.json()['data']['options'][0]
    assert option['kind'] == 'category'
    assert option['policy'] == {
        'enabled_override': None,
        'effective_enabled': True,
        'enabled_source': None,
        'margin_pct': None,
        'effective_margin_pct': '0',
        'margin_source': None,
    }
    assert not OzonCategoryPolicy.objects.exists()


@pytest.mark.django_db
def test_branch_and_leaf_overrides_inherit_inside_one_account(policy_setup):
    client = Client()
    token = policy_setup['token']
    account = policy_setup['account']
    revision = policy_setup['snapshot'].schema_hash

    root = _patch_policy(client, token, account, {
        'description_category_id': 101,
        'category_path_ids': [101],
        'tree_revision': revision,
        'enabled_override': False,
        'margin_pct': '12.50',
    })
    assert root.status_code == 200
    assert root.json()['data']['policy']['effective_enabled'] is False
    assert root.json()['data']['policy']['effective_margin_pct'] == '12.50'

    inherited = _tree_level(client, token, account, '101,102')
    assert inherited.status_code == 200
    inherited_options = {
        option['type_id']: option
        for option in inherited.json()['data']['options']
    }
    assert inherited_options[202]['policy']['effective_enabled'] is False
    assert inherited_options[202]['policy']['effective_margin_pct'] == '12.50'
    assert inherited_options[202]['policy']['enabled_source']['description_category_id'] == 101

    leaf = _patch_policy(client, token, account, {
        'description_category_id': 102,
        'type_id': 202,
        'category_path_ids': [101, 102],
        'tree_revision': revision,
        'enabled_override': True,
        'margin_pct': '20.00',
    })
    assert leaf.status_code == 200

    resolved = _tree_level(client, token, account, '101,102')
    options = {item['type_id']: item for item in resolved.json()['data']['options']}
    assert options[202]['policy']['effective_enabled'] is True
    assert options[202]['policy']['effective_margin_pct'] == '20.00'
    assert options[203]['policy']['effective_enabled'] is False
    assert options[203]['policy']['effective_margin_pct'] == '12.50'

    policies = OzonCategoryPolicy.objects.filter(account=account).order_by('type_id')
    assert policies.count() == 2
    assert all(policy.tenant_id == policy_setup['tenant'].pk for policy in policies)
    assert policies.get(type_id=202).margin_pct == Decimal('20.00')


@pytest.mark.django_db
def test_selected_offer_type_resolves_exact_nested_policy_path(policy_setup):
    account = policy_setup['account']
    revision = policy_setup['snapshot'].schema_hash
    OzonCategoryPolicy.objects.create(
        tenant=policy_setup['tenant'],
        account=account,
        description_category_id=101,
        enabled_override=False,
        margin_pct=Decimal('12.50'),
        category_path='Автотовары',
        node_name='Автотовары',
        tree_revision=revision,
    )
    OzonCategoryPolicy.objects.create(
        tenant=policy_setup['tenant'],
        account=account,
        description_category_id=102,
        type_id=202,
        enabled_override=True,
        margin_pct=Decimal('20.00'),
        category_path='Автотовары → Автозапчасти',
        node_name='Шланг тормозной',
        tree_revision=revision,
    )

    resolution = resolved_category_type_policy(
        account,
        description_category_id=102,
        type_id=202,
    )

    assert resolution['category_path_ids'] == [101, 102]
    assert resolution['category_path'] == 'Автотовары → Автозапчасти'
    assert resolution['type_name'] == 'Шланг тормозной'
    assert resolution['policy']['effective_enabled'] is True
    assert resolution['policy']['effective_margin_pct'] == '20.00'


@pytest.mark.django_db
def test_null_overrides_restore_inheritance_and_remove_empty_row(policy_setup):
    client = Client()
    token = policy_setup['token']
    account = policy_setup['account']
    payload = {
        'description_category_id': 101,
        'category_path_ids': [101],
        'tree_revision': policy_setup['snapshot'].schema_hash,
    }
    assert _patch_policy(client, token, account, {
        **payload,
        'enabled_override': False,
        'margin_pct': '15.00',
    }).status_code == 200

    reset = _patch_policy(client, token, account, {
        **payload,
        'enabled_override': None,
        'margin_pct': None,
    })

    assert reset.status_code == 200
    assert reset.json()['data']['stored_policy'] is None
    assert reset.json()['data']['policy']['effective_enabled'] is True
    assert reset.json()['data']['policy']['effective_margin_pct'] == '0'
    assert not OzonCategoryPolicy.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_policy_is_fenced_by_tenant_provider_account_and_tree_revision(policy_setup):
    client = Client()
    account = policy_setup['account']
    payload = {
        'description_category_id': 101,
        'category_path_ids': [101],
        'tree_revision': policy_setup['snapshot'].schema_hash,
        'enabled_override': False,
    }

    foreign = _patch_policy(
        client,
        policy_setup['other_token'],
        account,
        payload,
    )
    avito = _account(policy_setup['tenant'], 'policy-avito', marketplace='avito')
    wrong_provider = _patch_policy(client, policy_setup['token'], avito, payload)
    stale = _patch_policy(client, policy_setup['token'], account, {
        **payload,
        'tree_revision': 'e' * 64,
    })

    assert foreign.status_code == 404
    assert wrong_provider.status_code == 404
    assert stale.status_code == 409
    assert stale.json()['code'] == 'tree_revision_outdated'
    assert not OzonCategoryPolicy.objects.exists()


@pytest.mark.django_db
def test_policy_does_not_leak_to_second_account_or_mutate_snapshots_and_feeds(
    policy_setup,
):
    client = Client()
    account = policy_setup['account']
    original_tree = deepcopy(policy_setup['snapshot'].tree)
    payload = {
        'description_category_id': 101,
        'category_path_ids': [101],
        'tree_revision': policy_setup['snapshot'].schema_hash,
        'enabled_override': False,
        'margin_pct': '18.00',
    }

    with patch(TREE_READ) as provider_read:
        updated = _patch_policy(client, policy_setup['token'], account, payload)
        other_account_tree = _tree_level(
            client,
            policy_setup['token'],
            policy_setup['second_account'],
        )

    assert updated.status_code == other_account_tree.status_code == 200
    assert other_account_tree.json()['data']['options'][0]['policy']['effective_enabled'] is True
    provider_read.assert_not_called()
    policy_setup['snapshot'].refresh_from_db()
    assert policy_setup['snapshot'].tree == original_tree
    assert MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_invalid_path_and_unknown_fields_fail_without_side_effect(policy_setup):
    client = Client()
    base = {
        'description_category_id': 102,
        'category_path_ids': [102],
        'tree_revision': policy_setup['snapshot'].schema_hash,
        'enabled_override': False,
    }

    with patch(TREE_READ) as provider_read:
        invalid_path = _patch_policy(
            client,
            policy_setup['token'],
            policy_setup['account'],
            base,
        )
        unknown = _patch_policy(
            client,
            policy_setup['token'],
            policy_setup['account'],
            {**base, 'avito_category_id': 777},
        )

    assert invalid_path.status_code == 400
    assert invalid_path.json()['code'] == 'invalid_category_path'
    assert unknown.status_code == 400
    assert not OzonCategoryPolicy.objects.exists()
    provider_read.assert_not_called()
