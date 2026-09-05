from unittest.mock import patch
import uuid

import pytest
from django.test import Client

from apps.marketplaces.adapters.ozon.client import OzonAPIError
from apps.marketplaces.models import Listing, MarketplaceFeedRun, OzonOfferDraft, OzonOperation
from apps.marketplaces.tests.test_ozon_offers import (
    _account,
    _catalog,
    _complete_product,
    _product,
    _ready_offer,
    _request,
    _tenant,
)


PROVIDER_IMPORT = (
    'apps.marketplaces.ozon_publication.OzonSellerClient.import_products'
)
PUBLIC_IMAGE = 'apps.marketplaces.ozon_publication._public_image_url'


def _enable_write(settings, tenant, account):
    settings.OZON_ACCOUNT_CONNECTION_ENABLED = True
    settings.OZON_ACCOUNT_CONNECTION_TENANT_SLUGS = (tenant.slug,)
    settings.OZON_ACCOUNT_CONNECTION_CLIENT_IDS = (account.external_id,)
    profile = account.ozon_profile
    profile.product_write_enabled = True
    profile.api_methods = ['/v3/product/import']
    profile.save(update_fields=['product_write_enabled', 'api_methods', 'updated_at'])


def _publish(client, key, product, account, idempotency_key=None):
    return client.post(
        f'/api/v1/products/{product.pk}/ozon-offer/publish/',
        {
            'account_id': account.pk,
            'idempotency_key': str(idempotency_key or uuid.uuid4()),
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )


@pytest.mark.django_db
def test_manual_product_import_is_complete_durable_and_idempotent(settings):
    tenant, key = _tenant('ozon-publish-success')
    account = _account(tenant, 'client-publish-success')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    ready = _ready_offer(client, key, product, account)
    assert ready.json()['data']['preflight']['ready'] is True
    product.physical_profile.source_barcode = ''
    product.physical_profile.save(update_fields=['source_barcode', 'updated_at'])
    _enable_write(settings, tenant, account)
    idempotency_key = uuid.uuid4()

    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT, return_value='task-731') as provider_import,
    ):
        first = _publish(client, key, product, account, idempotency_key)
        repeated = _publish(client, key, product, account, idempotency_key)
        second_button_click = _publish(client, key, product, account)

    assert first.status_code == repeated.status_code == second_button_click.status_code == 202
    operation = OzonOperation.objects.get()
    assert operation.state == OzonOperation.State.RECONCILING
    assert operation.provider_task_id == 'task-731'
    assert operation.attempt_count == 1
    assert operation.request_sha256 and len(operation.request_sha256) == 64
    assert first.json()['operation_id'] == repeated.json()['operation_id']
    assert second_button_click.json()['operation_id'] == first.json()['operation_id']
    assert first.json()['data']['publication']['status'] == 'import_processing'
    assert first.json()['data']['publication']['write_enabled'] is True
    provider_import.assert_called_once()
    item = provider_import.call_args.args[0][0]
    assert item == {
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{'value': 'Canonical Brand', 'dictionary_value_id': 501}],
        }],
        'currency_code': 'RUB',
        'depth': 100,
        'description_category_id': 101,
        'dimension_unit': 'mm',
        'height': 50,
        'images': ['https://cdn.example.test/product.jpg'],
        'name': 'Амортизатор',
        'description': 'Описание товара',
        'offer_id': item['offer_id'],
        'price': '1000.00',
        'primary_image': 'https://cdn.example.test/product.jpg',
        'type_id': 202,
        'vat': '0.2',
        'weight': 500,
        'weight_unit': 'g',
        'width': 80,
    }
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    draft.provider_product_id = 77
    draft.save(update_fields=['provider_product_id', 'updated_at'])
    account.ozon_profile.api_methods.append('/v1/barcode/generate')
    account.ozon_profile.save(update_fields=['api_methods', 'updated_at'])
    with (
        patch('apps.marketplaces.ozon_publication.OzonSellerClient.generate_barcodes',
              return_value=[]),
        patch('apps.marketplaces.ozon_publication.OzonSellerClient.get_product_info_by_offer_id',
              return_value={'barcodes': ['OZN123456789']}),
    ):
        barcode_response = client.post(
            f'/api/v1/products/{product.pk}/ozon-offer/generate-barcode/',
            {'account_id': account.pk}, content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )
    assert barcode_response.status_code == 202
    assert barcode_response.json()['data']['publication']['barcode']['provider_values'] == [
        'OZN123456789',
    ]
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_product_import_is_fail_closed_before_provider_call(settings):
    tenant, key = _tenant('ozon-publish-closed')
    account = _account(tenant, 'client-publish-closed')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    _ready_offer(client, key, product, account)
    settings.OZON_ACCOUNT_CONNECTION_ENABLED = True
    settings.OZON_ACCOUNT_CONNECTION_TENANT_SLUGS = (tenant.slug,)
    settings.OZON_ACCOUNT_CONNECTION_CLIENT_IDS = (account.external_id,)

    with patch(PROVIDER_IMPORT) as provider_import:
        response = _publish(client, key, product, account)

    assert response.status_code == 400
    assert response.json()['code'] == 'write_disabled'
    assert OzonOperation.objects.count() == 0
    provider_import.assert_not_called()


@pytest.mark.django_db
def test_product_import_rejects_small_image_before_provider_call(settings):
    tenant, key = _tenant('ozon-publish-small-image')
    account = _account(tenant, 'client-publish-small-image')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    _ready_offer(client, key, product, account)
    image = product.images.get()
    image.resolution_w = 178
    image.resolution_h = 136
    image.save(update_fields=['resolution_w', 'resolution_h'])
    _enable_write(settings, tenant, account)

    with patch(PROVIDER_IMPORT) as provider_import:
        response = _publish(client, key, product, account)

    assert response.status_code == 400
    assert response.json()['code'] == 'preflight_failed'
    assert OzonOperation.objects.count() == 0
    provider_import.assert_not_called()


@pytest.mark.django_db
def test_idempotency_key_cannot_be_reused_for_another_offer(settings):
    tenant, key = _tenant('ozon-publish-idempotency-scope')
    account = _account(tenant, 'client-publish-idempotency-scope')
    first_product = _product(tenant, article='OZ-FIRST')
    second_product = _product(tenant, article='OZ-SECOND')
    _catalog(account)
    client = Client()
    _ready_offer(client, key, first_product, account)
    _complete_product(second_product)
    _request(client, key, 'patch', second_product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    second_ready = _request(client, key, 'patch', second_product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{
                'value': 'Ignored browser text',
                'dictionary_value_id': 501,
            }],
        }],
    })
    assert second_ready.json()['data']['preflight']['ready'] is True
    _enable_write(settings, tenant, account)
    idempotency_key = uuid.uuid4()

    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT, return_value='task-one') as provider_import,
    ):
        first = _publish(client, key, first_product, account, idempotency_key)
        conflict = _publish(client, key, second_product, account, idempotency_key)

    assert first.status_code == 202
    assert conflict.status_code == 400
    assert conflict.json()['code'] == 'idempotency_conflict'
    assert OzonOperation.objects.count() == 1
    provider_import.assert_called_once()


@pytest.mark.django_db
def test_preflight_and_tenant_fences_prevent_ozon_mutation(settings):
    tenant, key = _tenant('ozon-publish-fence')
    other_tenant, _ = _tenant('ozon-publish-fence-other')
    account = _account(tenant, 'client-publish-fence')
    other_account = _account(other_tenant, 'client-publish-fence-other')
    product = _product(tenant)
    _enable_write(settings, tenant, account)
    client = Client()

    with patch(PROVIDER_IMPORT) as provider_import:
        incomplete = _publish(client, key, product, account)
        foreign = _publish(client, key, product, other_account)

    assert incomplete.status_code == 400
    assert incomplete.json()['code'] == 'draft_missing'
    assert foreign.status_code == 404
    assert OzonOperation.objects.count() == 0
    provider_import.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('provider_error', 'expected_state', 'expected_status'),
    [
        (
            OzonAPIError('connection_error', 'Не удалось связаться с Ozon Seller API.'),
            OzonOperation.State.OUTCOME_UNKNOWN,
            'outcome_unknown',
        ),
        (
            OzonAPIError(
                'rate_limited',
                'Ozon временно ограничил частоту запросов.',
                retry_after_seconds=17,
            ),
            OzonOperation.State.FAILED,
            'send_failed',
        ),
    ],
)
def test_provider_failure_is_recorded_without_blind_retry(
    settings,
    provider_error,
    expected_state,
    expected_status,
):
    suffix = expected_state.replace('_', '-')
    tenant, key = _tenant(f'ozon-publish-{suffix}')
    account = _account(tenant, f'client-publish-{suffix}')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    _ready_offer(client, key, product, account)
    _enable_write(settings, tenant, account)

    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT, side_effect=provider_error) as provider_import,
    ):
        response = _publish(client, key, product, account)

    assert response.status_code == 202
    operation = OzonOperation.objects.get()
    assert operation.state == expected_state
    assert operation.attempt_count == 1
    assert operation.offer.publication_status == expected_status
    assert operation.errors == [{
        'code': provider_error.code,
        'message': str(provider_error),
    }]
    if expected_state == OzonOperation.State.OUTCOME_UNKNOWN:
        assert operation.completed_at is None
    else:
        assert operation.completed_at is not None
        assert operation.retry_after_at is not None
    provider_import.assert_called_once()
