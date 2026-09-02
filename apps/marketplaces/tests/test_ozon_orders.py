from unittest.mock import patch

import pytest
from django.test import Client

from apps.marketplaces.models import OzonFbsPosting
from apps.marketplaces.tests.test_ozon_offers import _account, _tenant


LIST_CALL = 'apps.marketplaces.ozon_orders.OzonSellerClient.list_fbs_postings'


@pytest.mark.django_db
def test_fbs_orders_are_read_only_tenant_and_account_scoped(settings):
    tenant, key = _tenant('ozon-orders')
    account = _account(tenant, 'ozon-orders-client')
    settings.OZON_ACCOUNT_CONNECTION_ENABLED = True
    settings.OZON_ACCOUNT_CONNECTION_TENANT_SLUGS = (tenant.slug,)
    settings.OZON_ACCOUNT_CONNECTION_CLIENT_IDS = (account.external_id,)
    posting = {
        'posting_number': '12345-0001-1', 'status': 'awaiting_packaging',
        'substatus': 'posting_acceptance_in_progress',
        'in_process_at': '2026-09-02T09:00:00Z',
        'shipment_date': '2026-09-03T15:00:00Z',
        'delivery_method': {'warehouse_id': 42},
        'products': [{
            'offer_id': 'map-1', 'sku': 731, 'name': 'Амортизатор',
            'quantity': 2, 'price': '1000.00',
        }],
    }
    client = Client()
    with patch(LIST_CALL, return_value=([posting], False)) as provider:
        sync = client.post(
            f'/api/v1/accounts/{account.pk}/ozon-fbs-orders/', {},
            content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}',
        )
    response = client.get(
        f'/api/v1/accounts/{account.pk}/ozon-fbs-orders/',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )

    assert sync.status_code == 200
    assert sync.json()['data']['imported'] == 1
    assert response.status_code == 200
    assert response.json()['data'][0]['posting_number'] == '12345-0001-1'
    assert response.json()['data'][0]['products'][0] == {
        'offer_id': 'map-1', 'sku': '731', 'name': 'Амортизатор',
        'quantity': 2, 'price': '1000.00',
    }
    assert OzonFbsPosting.objects.get().tenant == tenant
    provider.assert_called_once()


@pytest.mark.django_db
def test_fbs_order_endpoint_rejects_foreign_account():
    tenant, key = _tenant('ozon-orders-owner')
    other, _ = _tenant('ozon-orders-other')
    account = _account(other, 'ozon-orders-foreign')
    client = Client()
    with patch(LIST_CALL) as provider:
        response = client.post(
            f'/api/v1/accounts/{account.pk}/ozon-fbs-orders/', {},
            content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}',
        )
    assert response.status_code == 404
    provider.assert_not_called()
