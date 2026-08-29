from unittest.mock import patch

import pytest
from django.test import Client, override_settings

from apps.datasources.encryption import decrypt
from apps.marketplaces.adapters.ozon.client import (
    OzonConnectionSnapshot,
    OzonWarehouse,
)
from apps.marketplaces.models import MarketplaceAccount, OzonAccountProfile
from apps.marketplaces.account_errors import MarketplaceConnectionError
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


OZON_VERIFY = (
    'apps.marketplaces.ozon_account_connection.'
    'OzonAccountConnectionService._verify_connection'
)


def make_tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    return tenant, owner_access_token(tenant)


def snapshot(*warehouses: OzonWarehouse) -> OzonConnectionSnapshot:
    return OzonConnectionSnapshot(
        company_name='АльфаПро',
        seller_name='Alfa Seller',
        currency='RUB',
        roles=('Product API', 'FBS'),
        warehouses=warehouses,
    )


def ozon_payload(client_id: str = 'ozon-client-1', api_key: str = 'ozon-secret'):
    return {
        'name': f'Ozon {client_id}',
        'marketplace': 'ozon',
        'client_id': client_id,
        'api_key': api_key,
    }


@pytest.mark.django_db
@pytest.mark.parametrize('enabled', [False, True])
def test_provider_rollout_state_is_read_only_and_fail_closed_for_ui(settings, enabled):
    settings.OZON_ACCOUNT_CONNECTION_ENABLED = enabled
    _, token = make_tenant(f'ozon-rollout-{enabled}')
    with patch(
        'apps.marketplaces.adapters.ozon.client.OzonSellerClient.verify_connection',
    ) as provider_call:
        response = Client().get(
            '/api/v1/accounts/provider-rollout/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    assert response.status_code == 200
    assert response.json() == {
        'ozon': {
            'account_connection_enabled': enabled,
            'credential_update_enabled': enabled,
        },
    }
    provider_call.assert_not_called()


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=False)
def test_ozon_connection_is_dark_by_default_and_makes_no_provider_call():
    tenant, token = make_tenant('ozon-dark')
    with patch(
        'apps.marketplaces.adapters.ozon.client.OzonSellerClient.verify_connection',
    ) as provider_call:
        response = Client().post(
            '/api/v1/accounts/',
            ozon_payload(),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    assert response.status_code == 503
    assert response.json()['code'] == 'provider_disabled'
    provider_call.assert_not_called()
    assert not MarketplaceAccount.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_create_ozon_account_encrypts_api_key_and_returns_safe_profile():
    tenant, token = make_tenant('ozon-create')
    verified = snapshot(OzonWarehouse('warehouse-1', 'Основной склад'))
    with patch(OZON_VERIFY, return_value=verified):
        response = Client().post(
            '/api/v1/accounts/',
            ozon_payload(),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    assert response.status_code == 201
    body = response.json()
    assert body['marketplace'] == 'ozon'
    assert body['external_id'] == 'ozon-client-1'
    assert body['provider_capabilities']['account_health'] is True
    assert body['avito_status'] is None
    assert body['ozon_profile'] == {
        'connection_status': 'connected',
        'company_name': 'АльфаПро',
        'seller_name': 'Alfa Seller',
        'currency': 'RUB',
        'roles': ['Product API', 'FBS'],
        'api_methods': [],
        'api_key_expires_at': None,
        'warehouse_count': 1,
        'selected_warehouse_id': 'warehouse-1',
        'selected_warehouse_name': 'Основной склад',
        'last_checked_at': body['ozon_profile']['last_checked_at'],
    }
    assert 'api_key' not in body
    assert 'client_id' not in body
    account = MarketplaceAccount.objects.get(tenant=tenant)
    credentials = decrypt(bytes(account.credentials_enc))
    assert credentials == {
        'client_id': 'ozon-client-1',
        'api_key': 'ozon-secret',
    }
    assert b'ozon-secret' not in bytes(account.credentials_enc)


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_one_tenant_can_connect_multiple_ozon_accounts_and_tenants_are_isolated():
    first_tenant, first_token = make_tenant('ozon-multi-first')
    second_tenant, second_token = make_tenant('ozon-multi-second')
    verified = snapshot(OzonWarehouse('warehouse-1', 'Основной'))
    with patch(OZON_VERIFY, return_value=verified):
        first = Client().post(
            '/api/v1/accounts/', ozon_payload('client-a'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {first_token}',
        )
        second = Client().post(
            '/api/v1/accounts/', ozon_payload('client-b'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {first_token}',
        )
        foreign_duplicate = Client().post(
            '/api/v1/accounts/', ozon_payload('client-a'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {second_token}',
        )
        other_tenant = Client().post(
            '/api/v1/accounts/', ozon_payload('client-c'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {second_token}',
        )

    assert [first.status_code, second.status_code, other_tenant.status_code] == [201, 201, 201]
    assert foreign_duplicate.status_code == 409
    assert 'account' not in foreign_duplicate.json()
    assert MarketplaceAccount.objects.filter(
        tenant=first_tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_OZON,
    ).count() == 2
    response = Client().get(
        '/api/v1/accounts/?marketplace=ozon',
        HTTP_AUTHORIZATION=f'Bearer {first_token}',
    )
    assert {row['external_id'] for row in response.json()} == {'client-a', 'client-b'}
    assert other_tenant.json()['external_id'] == 'client-c'
    assert MarketplaceAccount.objects.filter(tenant=second_tenant).count() == 1


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_duplicate_ozon_account_is_rejected_inside_same_tenant():
    tenant, token = make_tenant('ozon-duplicate')
    verified = snapshot(OzonWarehouse('warehouse-1', 'Основной'))
    with patch(OZON_VERIFY, return_value=verified):
        Client().post(
            '/api/v1/accounts/', ozon_payload(),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        duplicate = Client().post(
            '/api/v1/accounts/', ozon_payload(api_key='rotated'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    assert duplicate.status_code == 409
    assert duplicate.json()['code'] == 'account_exists'
    assert duplicate.json()['account']['ozon_profile']['company_name'] == 'АльфаПро'
    assert MarketplaceAccount.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_multiple_warehouses_require_explicit_selection_instead_of_guessing():
    tenant, token = make_tenant('ozon-warehouses')
    verified = snapshot(
        OzonWarehouse('warehouse-1', 'Москва'),
        OzonWarehouse('warehouse-2', 'Казань'),
    )
    with patch(OZON_VERIFY, return_value=verified):
        response = Client().post(
            '/api/v1/accounts/', ozon_payload(),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    profile = response.json()['ozon_profile']
    assert response.status_code == 201
    assert profile['connection_status'] == 'warehouse_selection_required'
    assert profile['warehouse_count'] == 2
    assert profile['selected_warehouse_id'] == ''
    assert OzonAccountProfile.objects.get(account__tenant=tenant).selected_warehouse_id == ''


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_missing_warehouse_is_visible_in_account_health():
    tenant, token = make_tenant('ozon-no-warehouse')
    with patch(OZON_VERIFY, return_value=snapshot()):
        response = Client().post(
            '/api/v1/accounts/', ozon_payload(),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    profile = response.json()['ozon_profile']
    assert response.status_code == 201
    assert profile['connection_status'] == 'warehouse_missing'
    assert profile['warehouse_count'] == 0
    assert profile['selected_warehouse_id'] == ''


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_rotating_ozon_key_requires_same_client_id_and_is_atomic():
    tenant, token = make_tenant('ozon-rotate')
    verified = snapshot(OzonWarehouse('warehouse-1', 'Основной'))
    with patch(OZON_VERIFY, return_value=verified):
        created = Client().post(
            '/api/v1/accounts/', ozon_payload(api_key='old-key'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        account_id = created.json()['id']
        mismatch = Client().put(
            f'/api/v1/accounts/{account_id}/',
            ozon_payload('different-client', 'wrong-key'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        rotated = Client().put(
            f'/api/v1/accounts/{account_id}/',
            ozon_payload(api_key='new-key'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    assert mismatch.status_code == 400
    assert rotated.status_code == 200
    account = MarketplaceAccount.objects.get(pk=account_id, tenant=tenant)
    assert decrypt(bytes(account.credentials_enc))['api_key'] == 'new-key'


@pytest.mark.django_db
@override_settings(OZON_ACCOUNT_CONNECTION_ENABLED=True)
def test_ozon_rate_limit_is_exposed_as_safe_429():
    tenant, token = make_tenant('ozon-rate-limit')
    with patch(
        OZON_VERIFY,
        side_effect=MarketplaceConnectionError(
            'Ozon временно ограничил частоту запросов.',
            code='rate_limited',
            retry_after_seconds=11,
        ),
    ):
        response = Client().post(
            '/api/v1/accounts/', ozon_payload(api_key='must-not-leak'),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    assert response.status_code == 429
    assert response.json() == {
        'status': 'error',
        'code': 'rate_limited',
        'message': 'Ozon временно ограничил частоту запросов.',
        'retry_after_seconds': 11,
    }
    assert 'must-not-leak' not in response.content.decode()
    assert not MarketplaceAccount.objects.filter(tenant=tenant).exists()
