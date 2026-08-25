from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.products.tasks import sync_product_listings_task
from apps.tenants.models import Tenant


def make_listing():
    suffix = uuid4().hex[:10]
    tenant = Tenant.objects.create(name=f'Tenant {suffix}', slug=f'tenant-{suffix}')
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Provider account',
        external_id=f'account-{suffix}',
        credentials_enc=b'encrypted',
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'ARTICLE-{suffix}',
        name='Product',
        brand='Brand',
        price=Decimal('1250.00'),
        stock_qty=3,
    )
    listing = Listing.objects.create(
        tenant=tenant,
        account=account,
        product=product,
        status=Listing.STATUS_ACTIVE,
        external_id='item-1',
        external_url='https://www.avito.ru/items/item-1',
        price_on_listing=Decimal('1200.00'),
        remote_status=Listing.REMOTE_STATUS_ACTIVE,
        remote_status_checked_at=timezone.now() - timedelta(minutes=3),
        next_status_check_at=timezone.now() - timedelta(minutes=1),
        status_check_claim_token=uuid4(),
        status_check_claimed_until=timezone.now() + timedelta(minutes=5),
    )
    return account, product, listing


def assert_short_due(listing):
    assert listing.next_status_check_at is not None
    delta = listing.next_status_check_at - timezone.now()
    assert timedelta(minutes=9, seconds=50) <= delta <= timedelta(minutes=10)


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
@pytest.mark.parametrize('change_type', ['price_only', 'content', 'category'])
def test_active_product_content_intent_fences_claim(change_type):
    account, product, listing = make_listing()
    product.price = Decimal('1550.00')
    product.save(update_fields=['price'])

    with patch('apps.marketplaces.tasks.update_price_task.delay') as price_task, \
            patch('apps.marketplaces.tasks.update_listing_task.delay') as update_task, \
            patch('apps.notifications.tasks.send_notification_task.delay'):
        sync_product_listings_task(product.pk, change_type)

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.status == Listing.STATUS_ACTIVE
    assert listing.price_on_listing == Decimal('1550.00')
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None
    assert_short_due(listing)
    assert account.status_batch_due_at == listing.next_status_check_at
    if change_type == 'price_only':
        price_task.assert_called_once_with(listing.pk)
        update_task.assert_not_called()
    else:
        update_task.assert_called_once_with(listing.pk)
        price_task.assert_not_called()


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
def test_stock_zero_sets_archiving_once_and_fences_claim():
    account, product, listing = make_listing()
    product.stock_qty = 0
    product.save(update_fields=['stock_qty'])

    with patch('apps.marketplaces.tasks.unpublish_listing_task.delay') as unpublish, \
            patch('apps.notifications.tasks.send_notification_task.delay'):
        sync_product_listings_task(product.pk, 'stock_only')
        sync_product_listings_task(product.pk, 'stock_only')

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.status == Listing.STATUS_ARCHIVING
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None
    assert_short_due(listing)
    assert account.status_batch_due_at == listing.next_status_check_at
    unpublish.assert_called_once_with(listing.pk)


@pytest.mark.django_db
@override_settings(AVITO_STATUS_LIFECYCLE_MODE='dual_write')
@pytest.mark.parametrize('change_type', ['price_only', 'content', 'stock_only'])
def test_stale_product_sync_does_not_overwrite_or_enqueue(change_type):
    from apps.products import tasks as product_tasks

    _account, product, listing = make_listing()
    product.price = Decimal('1550.00')
    if change_type == 'stock_only':
        product.stock_qty = 0
    product.save(update_fields=['price', 'stock_qty'])
    original_apply = product_tasks._save_product_listing_intent

    def commit_newer_transition(stale_listing, update_fields, **kwargs):
        Listing.objects.filter(pk=stale_listing.pk).update(
            status=(
                Listing.STATUS_ARCHIVING
                if change_type == 'stock_only'
                else Listing.STATUS_DELETED
            ),
        )
        return original_apply(stale_listing, update_fields, **kwargs)

    with patch.object(
        product_tasks,
        '_save_product_listing_intent',
        side_effect=commit_newer_transition,
    ), patch(
        'apps.marketplaces.tasks.update_price_task.delay',
    ) as price_task, patch(
        'apps.marketplaces.tasks.update_listing_task.delay',
    ) as update_task, patch(
        'apps.marketplaces.tasks.unpublish_listing_task.delay',
    ) as unpublish_task, patch(
        'apps.notifications.tasks.send_notification_task.delay',
    ) as notification_task:
        sync_product_listings_task(product.pk, change_type)

    listing.refresh_from_db()
    assert listing.status == (
        Listing.STATUS_ARCHIVING
        if change_type == 'stock_only'
        else Listing.STATUS_DELETED
    )
    assert listing.price_on_listing == Decimal('1200.00')
    price_task.assert_not_called()
    update_task.assert_not_called()
    unpublish_task.assert_not_called()
    notification_task.assert_not_called()
