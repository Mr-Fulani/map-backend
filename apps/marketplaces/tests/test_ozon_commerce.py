from datetime import timedelta
from unittest.mock import patch
import uuid

import pytest
from django.test import Client
from django.utils import timezone

from apps.marketplaces.adapters.ozon.client import OzonAPIError
from apps.marketplaces.models import Listing, OzonOfferDraft, OzonOperation
from apps.marketplaces.tests.test_ozon_offers import (
    _account, _catalog, _product, _ready_offer, _tenant,
)
from apps.marketplaces.tests.test_ozon_publication import _enable_write


PRICE_CALL = 'apps.marketplaces.ozon_commerce.OzonSellerClient.update_prices'
STOCK_CALL = 'apps.marketplaces.ozon_commerce.OzonSellerClient.update_stocks'


def _setup(settings, slug='ozon-commerce'):
    tenant, key = _tenant(slug)
    account = _account(tenant, f'{slug}-client')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    _ready_offer(client, key, product, account)
    _enable_write(settings, tenant, account)
    profile = account.ozon_profile
    profile.selected_warehouse_id = '42'
    profile.selected_warehouse_name = 'Основной склад'
    profile.save(update_fields=['selected_warehouse_id', 'selected_warehouse_name', 'updated_at'])
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    draft.publication_status = 'published'
    draft.provider_product_id = 731
    draft.save(update_fields=['publication_status', 'provider_product_id', 'updated_at'])
    return tenant, key, account, product, draft, client


def _sync(client, key, product, account, token=None):
    return client.post(
        f'/api/v1/products/{product.pk}/ozon-offer/sync-commerce/',
        {'account_id': account.pk, 'idempotency_key': str(token or uuid.uuid4())},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )


@pytest.mark.django_db
def test_price_and_exact_warehouse_stock_are_confirmed_idempotently(settings):
    _, key, account, product, draft, client = _setup(settings)
    token = uuid.uuid4()
    with (
        patch(PRICE_CALL, return_value=[{
            'offer_id': draft.offer_id, 'product_id': 731, 'updated': True, 'errors': [],
        }]) as price_call,
        patch(STOCK_CALL, return_value=[{
            'offer_id': draft.offer_id, 'product_id': 731, 'warehouse_id': 42,
            'updated': True, 'errors': [],
        }]) as stock_call,
    ):
        first = _sync(client, key, product, account, token)
        repeated = _sync(client, key, product, account, token)

    assert first.status_code == repeated.status_code == 202
    assert OzonOperation.objects.filter(state=OzonOperation.State.SUCCEEDED).count() == 2
    price_call.assert_called_once()
    stock_call.assert_called_once()
    assert price_call.call_args.args[0][0]['offer_id'] == draft.offer_id
    assert stock_call.call_args.args[0][0] == {
        'offer_id': draft.offer_id, 'product_id': 731,
        'stock': product.stock_qty, 'warehouse_id': 42,
    }
    draft.refresh_from_db()
    assert str(draft.last_synced_price) == '1000.00'
    assert draft.last_synced_stock == product.stock_qty
    assert draft.last_stock_warehouse_id == '42'
    assert Listing.objects.count() == 0


@pytest.mark.django_db
def test_commerce_is_closed_before_publication_or_without_selected_warehouse(settings):
    _, key, account, product, draft, client = _setup(settings, 'ozon-commerce-closed')
    draft.publication_status = 'local_draft'
    draft.provider_product_id = None
    draft.save(update_fields=['publication_status', 'provider_product_id', 'updated_at'])
    with patch(PRICE_CALL) as price_call, patch(STOCK_CALL) as stock_call:
        response = _sync(client, key, product, account)
    assert response.status_code == 400
    assert response.json()['code'] == 'offer_not_published'
    assert OzonOperation.objects.count() == 0
    price_call.assert_not_called()
    stock_call.assert_not_called()


@pytest.mark.django_db
def test_ambiguous_price_response_is_not_retried_blindly(settings):
    _, key, account, product, _, client = _setup(settings, 'ozon-commerce-unknown')
    token = uuid.uuid4()
    failure = OzonAPIError('connection_error', 'Не удалось связаться с Ozon Seller API.')
    with patch(PRICE_CALL, side_effect=failure) as price_call, patch(
        STOCK_CALL,
        return_value=[{
            'offer_id': OzonOfferDraft.objects.get(product=product).offer_id,
            'warehouse_id': 42, 'updated': True, 'errors': [],
        }],
    ):
        first = _sync(client, key, product, account, token)
        repeated = _sync(client, key, product, account, token)
    assert first.status_code == repeated.status_code == 202
    operation = OzonOperation.objects.get(kind=OzonOperation.Kind.PRICE_UPDATE)
    assert operation.state == OzonOperation.State.OUTCOME_UNKNOWN
    assert operation.attempt_count == 1
    price_call.assert_called_once()


@pytest.mark.django_db
def test_ambiguous_stock_can_be_retried_manually_without_hiding_publication(settings):
    tenant, key, account, product, draft, client = _setup(
        settings, 'ozon-commerce-stock-retry',
    )
    published_operation = OzonOperation.objects.create(
        tenant=tenant,
        account=account,
        offer=draft,
        kind=OzonOperation.Kind.PRODUCT_IMPORT,
        state=OzonOperation.State.SUCCEEDED,
        idempotency_key='published-product-import',
        request_sha256='a' * 64,
        provider_task_id='task-published',
        completed_at=timezone.now(),
    )
    failure = OzonAPIError('connection_error', 'Не удалось связаться с Ozon Seller API.')
    stock_result = [{
        'offer_id': draft.offer_id,
        'product_id': 731,
        'warehouse_id': 42,
        'updated': True,
        'errors': [],
    }]

    with (
        patch(PRICE_CALL, return_value=[{
            'offer_id': draft.offer_id,
            'product_id': 731,
            'updated': True,
            'errors': [],
        }]) as price_call,
        patch(STOCK_CALL, side_effect=[failure, stock_result]) as stock_call,
    ):
        first = _sync(client, key, product, account, uuid.uuid4())
        assert first.status_code == 202
        assert first.json()['data']['publication']['latest_operation']['id'] == str(
            published_operation.pk,
        )
        unknown = OzonOperation.objects.get(kind=OzonOperation.Kind.STOCK_UPDATE)
        assert unknown.state == OzonOperation.State.OUTCOME_UNKNOWN

        too_soon = _sync(client, key, product, account, uuid.uuid4())
        assert too_soon.status_code == 400
        assert too_soon.json()['code'] == 'stock_cooldown'
        assert stock_call.call_count == 1

        unknown.last_attempt_at = timezone.now() - timedelta(seconds=31)
        unknown.save(update_fields=['last_attempt_at', 'updated_at'])

        second = _sync(client, key, product, account, uuid.uuid4())

    assert second.status_code == 202
    assert second.json()['data']['publication']['latest_operation']['id'] == str(
        published_operation.pk,
    )
    assert second.json()['data']['commerce']['last_synced_stock'] == product.stock_qty
    assert list(OzonOperation.objects.filter(
        kind=OzonOperation.Kind.STOCK_UPDATE,
    ).order_by('created_at').values_list('state', flat=True)) == [
        OzonOperation.State.OUTCOME_UNKNOWN,
        OzonOperation.State.SUCCEEDED,
    ]
    price_call.assert_called_once()
    assert stock_call.call_count == 2
