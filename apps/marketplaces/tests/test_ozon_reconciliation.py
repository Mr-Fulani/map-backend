from datetime import timedelta
from unittest.mock import patch
import uuid

import pytest
from django.test import Client
from django.utils import timezone

from apps.marketplaces.adapters.ozon.client import OzonAPIError
from apps.marketplaces.models import OzonOfferDraft, OzonOperation
from apps.marketplaces.ozon_reconciliation import reconcile_product_import
from apps.marketplaces.tests.test_ozon_offers import (
    _account,
    _catalog,
    _product,
    _ready_offer,
    _request,
    _tenant,
)
from apps.marketplaces.tests.test_ozon_publication import (
    PROVIDER_IMPORT,
    PUBLIC_IMAGE,
    _enable_write,
    _publish,
)


TASK_INFO = (
    'apps.marketplaces.ozon_reconciliation.'
    'OzonSellerClient.get_product_import_info'
)
PRODUCT_INFO = (
    'apps.marketplaces.ozon_reconciliation.'
    'OzonSellerClient.get_product_info_by_offer_id'
)


def _setup_sent_offer(settings, slug):
    tenant, key = _tenant(slug)
    account = _account(tenant, f'client-{slug}')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    _ready_offer(client, key, product, account)
    _enable_write(settings, tenant, account)
    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT, return_value='task-1'),
    ):
        sent = _publish(client, key, product, account)
    assert sent.status_code == 202
    return tenant, key, account, product, client


def _reconcile(client, key, product, account):
    return client.post(
        f'/api/v1/products/{product.pk}/ozon-offer/reconcile/',
        {'account_id': account.pk},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )


@pytest.mark.django_db
def test_import_and_moderation_success_are_projected_to_offer(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-reconcile-success',
    )
    draft = OzonOfferDraft.objects.get(product=product, account=account)

    with (
        patch(TASK_INFO, return_value={'items': [{
            'offer_id': draft.offer_id,
            'status': 'imported',
            'errors': [],
        }]}) as task_info,
        patch(PRODUCT_INFO, return_value={
            'id': 71,
            'sku': 801,
            'barcodes': ['OZN123456789'],
            'offer_id': draft.offer_id,
            'errors': [],
            'statuses': {
                'status': 'processed',
                'moderate_status': 'approved',
                'validation_status': 'success',
            },
        }) as product_info,
    ):
        response = _reconcile(client, key, product, account)

    assert response.status_code == 200
    data = response.json()['data']['publication']
    assert data['status'] == 'published'
    assert data['provider_product_id'] == 71
    assert data['provider_sku'] == 801
    assert data['barcode']['provider_values'] == ['OZN123456789']
    assert data['provider_status'] == 'processed'
    assert data['moderation_status'] == 'approved'
    assert data['latest_operation']['state'] == 'succeeded'
    assert data['latest_operation']['reconcile_count'] == 1
    task_info.assert_called_once_with('task-1')
    product_info.assert_called_once_with(draft.offer_id)


@pytest.mark.django_db
def test_approved_product_image_errors_are_nonblocking_deduplicated_warnings(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-reconcile-approved-image-warning',
    )
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    with (
        patch(TASK_INFO, return_value={'items': [{
            'offer_id': draft.offer_id,
            'status': 'imported',
            'errors': [],
        }]}),
        patch(PRODUCT_INFO, return_value={
            'id': 3177,
            'sku': 5003177,
            'offer_id': draft.offer_id,
            'errors': [{
                'code': 'pics_invalid_dimensions',
                'field': 'pictures',
                'description': 'Invalid image dimensions',
            }, {
                'code': 'some_image_failed',
                'field': 'pictures',
                'description': 'Some image failed',
            }],
            'statuses': {
                'status': 'price_sent',
                'moderate_status': 'approved',
                'validation_status': 'success',
            },
        }),
    ):
        response = _reconcile(client, key, product, account)

    assert response.status_code == 200
    publication = response.json()['data']['publication']
    assert publication['status'] == 'published'
    assert publication['provider_product_id'] == 3177
    assert publication['provider_sku'] == 5003177
    assert publication['latest_operation']['state'] == 'succeeded'
    assert publication['latest_operation']['errors'] == []
    assert publication['provider_errors'] == [{
        'code': 'provider_warning',
        'provider_code': 'pics_invalid_dimensions',
        'field': 'pictures',
        'attribute_id': None,
        'message': (
            'Ozon принял карточку, но отклонил одно или несколько изображений. '
            'Проверьте требования к фотографиям.'
        ),
        'severity': 'warning',
        'blocking': False,
    }]


@pytest.mark.django_db
def test_manual_refresh_can_heal_terminal_product_projection(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-reconcile-terminal-refresh',
    )
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    operation = OzonOperation.objects.get()
    operation.state = OzonOperation.State.FAILED
    operation.errors = [{'code': 'moderation_rejected', 'message': 'Старая ошибка.'}]
    operation.completed_at = timezone.now()
    operation.next_reconcile_at = None
    operation.save(update_fields=['state', 'errors', 'completed_at', 'next_reconcile_at'])
    draft.publication_status = 'moderation_failed'
    draft.provider_errors = operation.errors
    draft.save(update_fields=['publication_status', 'provider_errors'])

    # Background reconciliation never reopens terminal work on its own.
    with (patch(TASK_INFO) as task_info, patch(PRODUCT_INFO) as product_info):
        unchanged = reconcile_product_import(product, account)
    assert unchanged.state == OzonOperation.State.FAILED
    task_info.assert_not_called()
    product_info.assert_not_called()

    with (
        patch(TASK_INFO, return_value={'items': [{
            'offer_id': draft.offer_id,
            'status': 'imported',
            'errors': [],
        }]}),
        patch(PRODUCT_INFO, return_value={
            'id': 88,
            'sku': 99,
            'offer_id': draft.offer_id,
            'errors': [],
            'statuses': {
                'status': 'processed',
                'moderate_status': 'approved',
                'validation_status': 'success',
            },
        }),
    ):
        refreshed = _reconcile(client, key, product, account)

    assert refreshed.status_code == 200
    publication = refreshed.json()['data']['publication']
    assert publication['status'] == 'published'
    assert publication['provider_product_id'] == 88
    assert publication['provider_sku'] == 99
    assert publication['provider_errors'] == []
    assert publication['latest_operation']['state'] == 'succeeded'


@pytest.mark.django_db
def test_pending_import_is_rate_limited_and_never_reads_product_early(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-reconcile-pending',
    )
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    task_result = {'items': [{
        'offer_id': draft.offer_id,
        'status': 'pending',
        'errors': [],
    }]}

    with (
        patch(TASK_INFO, return_value=task_result) as task_info,
        patch(PRODUCT_INFO) as product_info,
    ):
        first = _reconcile(client, key, product, account)
        immediate_repeat = _reconcile(client, key, product, account)

    assert first.status_code == immediate_repeat.status_code == 200
    operation = OzonOperation.objects.get()
    assert operation.state == OzonOperation.State.RECONCILING
    assert operation.reconcile_count == 1
    assert operation.next_reconcile_at and operation.next_reconcile_at > timezone.now()
    task_info.assert_called_once_with('task-1')
    product_info.assert_not_called()


@pytest.mark.django_db
def test_import_rejection_is_explained_and_requires_a_changed_card(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-reconcile-rejected',
    )
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    with patch(TASK_INFO, return_value={'items': [{
        'offer_id': draft.offer_id,
        'status': 'failed',
        'errors': [{
            'code': 'ERROR_ATTRIBUTE_REQUIRED',
            'attribute_id': 85,
            'field': 'attributes',
            'description': 'Required attribute is missing',
        }],
    }]}):
        rejected = _reconcile(client, key, product, account)

    assert rejected.status_code == 200
    operation = OzonOperation.objects.get()
    assert operation.state == OzonOperation.State.FAILED
    assert operation.errors == [{
        'code': 'import_failed',
        'provider_code': 'ERROR_ATTRIBUTE_REQUIRED',
        'field': 'attributes',
        'attribute_id': 85,
        'message': 'Заполните обязательное поле или характеристику Ozon.',
    }]
    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT) as provider_import,
    ):
        unchanged = _publish(client, key, product, account, uuid.uuid4())
    assert unchanged.status_code == 400
    assert unchanged.json()['code'] == 'correction_required'
    provider_import.assert_not_called()

    changed = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'price_override': '1100.00',
    })
    assert changed.status_code == 200
    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT, return_value='task-2') as provider_import,
    ):
        repeated = _publish(client, key, product, account, uuid.uuid4())
    assert repeated.status_code == 202
    assert OzonOperation.objects.count() == 2
    provider_import.assert_called_once()


@pytest.mark.django_db
def test_moderation_rejection_is_terminal_and_keeps_provider_details(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-moderation-rejected',
    )
    draft = OzonOfferDraft.objects.get(product=product, account=account)
    with (
        patch(TASK_INFO, return_value={'items': [{
            'offer_id': draft.offer_id,
            'status': 'imported',
            'errors': [],
        }]}),
        patch(PRODUCT_INFO, return_value={
            'id': 73,
            'offer_id': draft.offer_id,
            'errors': [{
                'code': 'IMAGE_INVALID',
                'field': 'images',
                'description': 'Image does not meet requirements',
            }],
            'statuses': {
                'status': 'failed',
                'moderate_status': 'declined',
            },
        }),
    ):
        response = _reconcile(client, key, product, account)

    assert response.status_code == 200
    data = response.json()['data']['publication']
    assert data['status'] == 'moderation_failed'
    assert data['provider_status'] == 'failed'
    assert data['moderation_status'] == 'declined'
    assert data['latest_operation']['state'] == 'failed'
    assert data['latest_operation']['errors'][0]['field'] == 'images'


@pytest.mark.django_db
def test_unknown_send_requires_multiple_delayed_negative_observations(settings):
    tenant, key = _tenant('ozon-reconcile-unknown')
    account = _account(tenant, 'client-ozon-reconcile-unknown')
    product = _product(tenant)
    _catalog(account)
    client = Client()
    _ready_offer(client, key, product, account)
    _enable_write(settings, tenant, account)
    with (
        patch(PUBLIC_IMAGE, return_value='https://cdn.example.test/product.jpg'),
        patch(PROVIDER_IMPORT, side_effect=OzonAPIError(
            'connection_error',
            'Не удалось связаться с Ozon Seller API.',
        )),
    ):
        sent = _publish(client, key, product, account)
    assert sent.json()['data']['publication']['latest_operation']['state'] == 'outcome_unknown'
    operation = OzonOperation.objects.get()
    OzonOperation.objects.filter(pk=operation.pk).update(
        created_at=timezone.now() - timedelta(minutes=3),
        reconcile_count=2,
        next_reconcile_at=None,
    )

    with (
        patch(TASK_INFO) as task_info,
        patch(PRODUCT_INFO, return_value=None) as product_info,
    ):
        response = _reconcile(client, key, product, account)

    assert response.status_code == 200
    operation.refresh_from_db()
    assert operation.state == OzonOperation.State.FAILED
    assert operation.errors[0]['code'] == 'provider_not_found'
    assert operation.offer.publication_status == 'not_accepted'
    task_info.assert_not_called()
    product_info.assert_called_once_with(operation.offer.offer_id)


@pytest.mark.django_db
def test_transient_status_error_preserves_reconciling_state(settings):
    _, key, account, product, client = _setup_sent_offer(
        settings,
        'ozon-reconcile-transient',
    )
    with patch(TASK_INFO, side_effect=OzonAPIError(
        'rate_limited',
        'Ozon временно ограничил частоту запросов.',
        retry_after_seconds=17,
    )):
        response = _reconcile(client, key, product, account)

    assert response.status_code == 200
    operation = OzonOperation.objects.get()
    assert operation.state == OzonOperation.State.RECONCILING
    assert operation.next_reconcile_at and operation.next_reconcile_at > timezone.now()
    assert operation.offer.provider_errors[0]['code'] == 'status_check_failed'
