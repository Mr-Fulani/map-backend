from datetime import timedelta
from unittest.mock import patch
import uuid

import pytest
from django.utils import timezone

from apps.marketplaces.adapters.ozon.client import OzonAPIError
from apps.marketplaces.models import OzonOperation
from apps.marketplaces.tests.test_ozon_commerce import STOCK_CALL, _setup
from apps.marketplaces.tests.test_ozon_publication import _publish


ARCHIVE_CALL = 'apps.marketplaces.ozon_lifecycle.OzonSellerClient.archive_products'
INFO_CALL = 'apps.marketplaces.ozon_lifecycle.OzonSellerClient.get_product_info_by_offer_id'


def _enable_archive(account):
    profile = account.ozon_profile
    profile.api_methods = [*profile.api_methods, '/v1/product/archive']
    profile.save(update_fields=['api_methods', 'updated_at'])


def _archive(client, key, product, account, token=None):
    return client.post(
        f'/api/v1/products/{product.pk}/ozon-offer/archive/',
        {'account_id': account.pk, 'idempotency_key': str(token or uuid.uuid4())},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )


def _zero_result(draft):
    return [{
        'offer_id': draft.offer_id,
        'product_id': draft.provider_product_id,
        'warehouse_id': 42,
        'updated': True,
        'errors': [],
    }]


def _provider_item(draft, *, archived):
    return {
        'id': draft.provider_product_id,
        'offer_id': draft.offer_id,
        'is_archived': archived,
    }


@pytest.mark.django_db
def test_archive_zeroes_exact_stock_then_archives_and_reconciles(settings):
    _, key, account, product, draft, client = _setup(settings, 'ozon-archive-success')
    _enable_archive(account)
    token = uuid.uuid4()

    with (
        patch(STOCK_CALL, return_value=_zero_result(draft)) as stock_call,
        patch(ARCHIVE_CALL, return_value=True) as archive_call,
        patch(INFO_CALL, return_value=_provider_item(draft, archived=True)) as info_call,
    ):
        first = _archive(client, key, product, account, token)
        repeated = _archive(client, key, product, account, token)

    assert first.status_code == repeated.status_code == 202
    operation = OzonOperation.objects.get(kind=OzonOperation.Kind.ARCHIVE)
    assert operation.state == OzonOperation.State.SUCCEEDED
    assert first.json()['operation_id'] == repeated.json()['operation_id']
    assert first.json()['data']['publication']['status'] == 'archived'
    assert first.json()['data']['publication']['latest_operation']['kind'] == 'archive'
    assert first.json()['data']['publication']['can_archive'] is False
    stock_call.assert_called_once_with([{
        'offer_id': draft.offer_id,
        'product_id': draft.provider_product_id,
        'warehouse_id': 42,
        'stock': 0,
    }])
    archive_call.assert_called_once_with([draft.provider_product_id])
    info_call.assert_called_once_with(draft.offer_id)
    draft.refresh_from_db()
    assert draft.last_synced_stock == 0
    assert draft.last_stock_warehouse_id == '42'
    assert draft.publication_status == 'archived'


@pytest.mark.django_db
def test_archive_stops_before_provider_archive_without_confirmed_zero_stock(settings):
    _, key, account, product, draft, client = _setup(settings, 'ozon-archive-stock-fail')
    _enable_archive(account)
    rejected = [{
        'offer_id': draft.offer_id,
        'product_id': draft.provider_product_id,
        'warehouse_id': 42,
        'updated': False,
        'errors': [{'code': 'REJECTED', 'message': 'Stock rejected'}],
    }]

    with (
        patch(STOCK_CALL, return_value=rejected),
        patch(ARCHIVE_CALL) as archive_call,
        patch(INFO_CALL) as info_call,
    ):
        response = _archive(client, key, product, account)

    assert response.status_code == 202
    operation = OzonOperation.objects.get(kind=OzonOperation.Kind.ARCHIVE)
    assert operation.state == OzonOperation.State.MANUAL_REVIEW
    assert operation.errors[0]['code'] == 'stock_zero_not_confirmed'
    archive_call.assert_not_called()
    info_call.assert_not_called()
    draft.refresh_from_db()
    assert draft.publication_status == 'archive_failed'


@pytest.mark.django_db
def test_ambiguous_archive_is_resolved_by_immediate_read(settings):
    _, key, account, product, draft, client = _setup(settings, 'ozon-archive-unknown')
    _enable_archive(account)
    failure = OzonAPIError('connection_error', 'Не удалось связаться с Ozon Seller API.')

    with (
        patch(STOCK_CALL, return_value=_zero_result(draft)),
        patch(ARCHIVE_CALL, side_effect=failure),
        patch(INFO_CALL, return_value=_provider_item(draft, archived=True)),
    ):
        response = _archive(client, key, product, account)

    assert response.status_code == 202
    operation = OzonOperation.objects.get(kind=OzonOperation.Kind.ARCHIVE)
    assert operation.state == OzonOperation.State.SUCCEEDED
    assert response.json()['data']['publication']['status'] == 'archived'


@pytest.mark.django_db
def test_archive_reconcile_endpoint_only_reads_until_provider_confirms(settings):
    _, key, account, product, draft, client = _setup(settings, 'ozon-archive-reconcile')
    _enable_archive(account)

    with (
        patch(STOCK_CALL, return_value=_zero_result(draft)),
        patch(ARCHIVE_CALL, return_value=True),
        patch(INFO_CALL, return_value=_provider_item(draft, archived=False)) as info_call,
    ):
        response = _archive(client, key, product, account)
    assert response.status_code == 202
    operation = OzonOperation.objects.get(kind=OzonOperation.Kind.ARCHIVE)
    assert operation.state == OzonOperation.State.RECONCILING
    operation.next_reconcile_at = timezone.now() - timedelta(seconds=1)
    operation.save(update_fields=['next_reconcile_at', 'updated_at'])

    with patch(INFO_CALL, return_value=_provider_item(draft, archived=True)) as second_info:
        reconciled = client.post(
            f'/api/v1/products/{product.pk}/ozon-offer/reconcile/',
            {'account_id': account.pk},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

    assert reconciled.status_code == 200
    assert reconciled.json()['data']['publication']['status'] == 'archived'
    second_info.assert_called_once_with(draft.offer_id)
    assert info_call.call_count == 1


@pytest.mark.django_db
def test_archive_is_fail_closed_without_exact_method_permission(settings):
    _, key, account, product, _, client = _setup(settings, 'ozon-archive-closed')

    with (
        patch(STOCK_CALL) as stock_call,
        patch(ARCHIVE_CALL) as archive_call,
    ):
        response = _archive(client, key, product, account)

    assert response.status_code == 400
    assert response.json()['code'] == 'archive_disabled'
    assert OzonOperation.objects.count() == 0
    stock_call.assert_not_called()
    archive_call.assert_not_called()


@pytest.mark.django_db
def test_archive_is_blocked_while_a_commerce_mutation_is_active(settings):
    tenant, key, account, product, draft, client = _setup(
        settings, 'ozon-archive-commerce-race',
    )
    _enable_archive(account)
    OzonOperation.objects.create(
        tenant=tenant,
        account=account,
        offer=draft,
        kind=OzonOperation.Kind.STOCK_UPDATE,
        state=OzonOperation.State.SENDING,
        idempotency_key='active-stock',
        request_sha256='a' * 64,
    )

    with patch(STOCK_CALL) as stock_call, patch(ARCHIVE_CALL) as archive_call:
        response = _archive(client, key, product, account)

    assert response.status_code == 400
    assert response.json()['code'] == 'commerce_in_progress'
    stock_call.assert_not_called()
    archive_call.assert_not_called()


@pytest.mark.django_db
def test_new_product_import_is_blocked_after_archive_confirmation(settings):
    _, key, account, product, draft, client = _setup(
        settings, 'ozon-archive-block-republish',
    )
    _enable_archive(account)
    with (
        patch(STOCK_CALL, return_value=_zero_result(draft)),
        patch(ARCHIVE_CALL, return_value=True),
        patch(INFO_CALL, return_value=_provider_item(draft, archived=True)),
    ):
        archived = _archive(client, key, product, account)
    assert archived.status_code == 202

    with patch(
        'apps.marketplaces.ozon_publication.OzonSellerClient.import_products',
    ) as provider_import:
        response = _publish(client, key, product, account)

    assert response.status_code == 400
    assert response.json()['code'] == 'offer_archived'
    provider_import.assert_not_called()
