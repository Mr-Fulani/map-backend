from unittest.mock import patch
import uuid

import pytest
from django.core.management import call_command
from django.test import Client
from django_celery_beat.models import PeriodicTask

from apps.marketplaces.models import OzonOfferDraft, OzonOperation
from apps.marketplaces.ozon_tasks import (
    sync_enabled_ozon_commerce, sync_enabled_ozon_orders,
)
from apps.marketplaces.tests.test_ozon_commerce import _setup
from apps.marketplaces.tests.test_ozon_offers import _account, _tenant


@pytest.mark.django_db
def test_automation_defaults_off_and_is_exact_account_scoped(settings):
    tenant, key = _tenant('ozon-auto')
    account = _account(tenant, 'ozon-auto-client')
    client = Client()
    profile = account.ozon_profile
    assert profile.commerce_auto_sync_enabled is False
    assert profile.orders_auto_sync_enabled is False

    response = client.patch(
        f'/api/v1/accounts/{account.pk}/ozon-automation/',
        {'orders_auto_sync_enabled': True}, content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )
    assert response.status_code == 200
    profile.refresh_from_db()
    assert profile.orders_auto_sync_enabled is True
    assert profile.commerce_auto_sync_enabled is False


@pytest.mark.django_db
def test_automation_tasks_only_visit_explicitly_enabled_accounts(settings):
    _, _, account, _, _, _ = _setup(settings, 'ozon-auto-tasks')
    with (
        patch('apps.marketplaces.ozon_tasks.sync_offer_commerce') as commerce,
        patch('apps.marketplaces.ozon_tasks.sync_fbs_orders') as orders,
    ):
        assert sync_enabled_ozon_commerce()['checked'] == 0
        assert sync_enabled_ozon_orders()['checked'] == 0
        commerce.assert_not_called()
        orders.assert_not_called()

        profile = account.ozon_profile
        profile.commerce_auto_sync_enabled = True
        profile.orders_auto_sync_enabled = True
        profile.save(update_fields=['commerce_auto_sync_enabled', 'orders_auto_sync_enabled', 'updated_at'])
        assert sync_enabled_ozon_commerce()['checked'] == 1
        assert sync_enabled_ozon_orders()['checked'] == 1
        commerce.assert_called_once()
        orders.assert_called_once_with(account)


@pytest.mark.django_db
def test_bulk_publish_is_bounded_and_keeps_per_product_operations(settings):
    _, key, account, product, draft, client = _setup(settings, 'ozon-auto-bulk')
    # Bulk publication requires a local ready draft, not an already-published state.
    draft.publication_status = 'local_draft'
    draft.provider_product_id = None
    draft.save(update_fields=['publication_status', 'provider_product_id', 'updated_at'])
    with (
        patch('apps.marketplaces.ozon_publication._public_image_url', return_value='https://cdn.example.test/x.jpg'),
        patch('apps.marketplaces.ozon_publication.OzonSellerClient.import_products', return_value='task-bulk') as provider,
    ):
        response = client.post(
            '/api/v1/products/ozon-offers/bulk/',
            {
                'account_id': account.pk, 'product_ids': [product.pk],
                'action': 'publish', 'idempotency_key': str(uuid.uuid4()),
            },
            content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}',
        )
    assert response.status_code == 202
    assert response.json()['data'][0]['status'] == 'accepted'
    assert OzonOperation.objects.filter(kind='product_import').count() == 1
    provider.assert_called_once()


@pytest.mark.django_db
def test_ozon_periodic_jobs_are_registered_without_changing_avito_jobs():
    call_command('setup_periodic_tasks')
    expected = {
        'reconcile_due_ozon_imports',
        'sync_enabled_ozon_commerce',
        'sync_enabled_ozon_orders',
    }
    assert set(PeriodicTask.objects.filter(name__in=expected).values_list('name', flat=True)) == expected
    assert PeriodicTask.objects.get(name='check_moderation_status').task == 'apps.marketplaces.tasks.check_moderation_status'
