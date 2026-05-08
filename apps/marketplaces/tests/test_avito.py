from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import (
    NotFoundError,
    TokenExpiredError,
    backoff,
)
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.services import ProductService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_account(tenant):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Account',
        external_id='12345',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csec'}),
    )


def make_product(tenant):
    ds = DataSourceConnection.objects.create(
        tenant=tenant, name='S', type='1c_http',
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    product, _ = ProductService.upsert_from_source(tenant, ds, {
        'uuid': None, 'article': 'ART-001', 'name': 'Тормозной диск',
        'brand': 'Bosch', 'price': '3500', 'stock_qty': 5,
        'category': 'Тормоза', 'condition': 'new',
    })
    return product


def make_listing(tenant, product, account):
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        price_on_listing=Decimal('3500'),
        title='Тормозной диск Bosch',
        description_ai='Описание тестовое',
        status=Listing.STATUS_DRAFT,
    )


@pytest.mark.django_db
class TestAvitoAuthManager:
    @responses_lib.activate
    def test_get_token_fetches_and_caches(self):
        responses_lib.add(
            responses_lib.POST, 'https://api.avito.ru/token',
            json={'access_token': 'tok123', 'expires_in': 3600},
            status=200,
        )
        tenant = make_tenant('auth-co')
        account = make_account(tenant)
        from django.core.cache import cache
        cache.clear()

        manager = AvitoAuthManager()
        token = manager.get_token(account)
        assert token == 'tok123'
        assert cache.get(f'avito:token:{account.pk}') == 'tok123'

    @responses_lib.activate
    def test_get_token_uses_cache_on_second_call(self):
        responses_lib.add(
            responses_lib.POST, 'https://api.avito.ru/token',
            json={'access_token': 'tok-fresh', 'expires_in': 3600},
            status=200,
        )
        tenant = make_tenant('cache-co')
        account = make_account(tenant)
        from django.core.cache import cache
        cache.clear()

        manager = AvitoAuthManager()
        manager.get_token(account)
        manager.get_token(account)
        assert len(responses_lib.calls) == 1


@pytest.mark.django_db
class TestPublishListingTask:
    def test_publish_idempotency(self):
        """Второй вызов задачи не создаёт новый листинг."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('idemp-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        listing.external_id = 'ext-already-set'
        listing.status = Listing.STATUS_ACTIVE
        listing.save()

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_adapter:
            publish_listing_task(listing.pk)
            mock_adapter.assert_not_called()

    def test_401_triggers_token_refresh(self):
        """После TokenExpiredError задача ставится на retry."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('token-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache') as mock_cache:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_cache.get.return_value = None
            mock_cls.return_value.publish.side_effect = TokenExpiredError()

            with pytest.raises((TokenExpiredError, Retry)):
                publish_listing_task(listing.pk)

    def test_404_triggers_republish(self):
        from apps.marketplaces.tasks import update_listing_task
        tenant = make_tenant('notfound-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        listing.external_id = 'ext-404'
        listing.status = Listing.STATUS_ACTIVE
        listing.save()

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.publish_listing_task') as mock_publish:
            mock_cls.return_value.update.side_effect = NotFoundError()
            update_listing_task(listing.pk)
            listing.refresh_from_db()
            assert listing.external_id is None
            assert listing.status == Listing.STATUS_DRAFT
            mock_publish.delay.assert_called_once_with(listing.pk)

    def test_429_applies_exponential_backoff(self):
        assert backoff(0) == 30
        assert backoff(1) == 60
        assert backoff(2) == 120
        assert backoff(3) == 300
        assert backoff(99) == 300

    def test_price_change_calls_patch_not_put(self):
        from apps.marketplaces.tasks import update_price_task
        tenant = make_tenant('price-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        listing.external_id = 'ext-price'
        listing.save()

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            mock_cls.return_value.update_price.return_value = None
            update_price_task(listing.pk)
            mock_cls.return_value.update_price.assert_called_once_with(listing)
            mock_cls.return_value.update.assert_not_called()

    def test_e2e_10_products_publish(self):
        """10 товаров публикуются через mock Avito → status=active."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('e2e-co')
        account = make_account(tenant)
        ds = DataSourceConnection.objects.create(
            tenant=tenant, name='S', type='1c_http',
            credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
        )

        listings = []
        for i in range(10):
            product, _ = ProductService.upsert_from_source(tenant, ds, {
                'uuid': None, 'article': f'P{i:03d}', 'name': f'Товар {i}',
                'brand': 'B', 'price': '100', 'stock_qty': 1,
                'category': 'Кузов', 'condition': 'new',
            })
            lst = make_listing(tenant, product, account)
            listings.append(lst)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache') as mock_cache:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_cache.get.return_value = None
            mock_cls.return_value.publish.side_effect = lambda lst: f'avito-{lst.pk}'

            for lst in listings:
                publish_listing_task(lst.pk)

        active = Listing.objects.filter(tenant=tenant, status=Listing.STATUS_ACTIVE).count()
        assert active == 10
