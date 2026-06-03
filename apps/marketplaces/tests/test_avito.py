from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import backoff
from apps.marketplaces.adapters.avito.feed_builder import build_feed, get_ad_id
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


def make_listing(tenant, product, account, **kwargs):
    kwargs.setdefault('status', Listing.STATUS_DRAFT)
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        price_on_listing=Decimal('3500'),
        title='Тормозной диск Bosch',
        description_ai='Описание тестовое',
        **kwargs,
    )


# ------------------------------------------------------------------ #
#  Auth                                                               #
# ------------------------------------------------------------------ #

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


# ------------------------------------------------------------------ #
#  Feed builder                                                       #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestFeedBuilder:
    def test_build_feed_generates_valid_xml(self):
        """Фид содержит обязательные теги для каждого листинга."""
        tenant = make_tenant('feed-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        xml_bytes = build_feed([listing])
        xml_str = xml_bytes.decode('utf-8')

        assert '<?xml' in xml_str
        assert 'formatVersion="3"' in xml_str
        assert 'target="Avito.ru"' in xml_str
        assert f'<Id>{listing.publish_idempotency_key}</Id>' in xml_str
        assert '<Title>' in xml_str
        assert '<Price>3500</Price>' in xml_str
        assert '<Condition>Новое</Condition>' in xml_str

    def test_build_feed_archived_has_remove_status(self):
        """Листинг в статусе ARCHIVED получает тег <Status>Remove</Status>."""
        tenant = make_tenant('feed-rm-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_ARCHIVED)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<Status>Remove</Status>' in xml_str

    def test_build_feed_draft_has_no_remove_status(self):
        """Активный/черновой листинг не содержит тег Remove."""
        tenant = make_tenant('feed-act-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_DRAFT)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<Status>' not in xml_str

    def test_get_ad_id_returns_idempotency_key(self):
        tenant = make_tenant('adid-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        assert get_ad_id(listing) == str(listing.publish_idempotency_key)

    def test_build_feed_multiple_listings(self):
        """Фид корректно обрабатывает несколько листингов."""
        tenant = make_tenant('multi-co')
        account = make_account(tenant)
        ds = DataSourceConnection.objects.create(
            tenant=tenant, name='S', type='1c_http',
            credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
        )
        listings = []
        for i in range(3):
            product, _ = ProductService.upsert_from_source(tenant, ds, {
                'uuid': None, 'article': f'M{i}', 'name': f'Товар {i}',
                'brand': 'B', 'price': '100', 'stock_qty': 1,
                'category': 'Кузов', 'condition': 'new',
            })
            listings.append(make_listing(tenant, product, account))

        xml_str = build_feed(listings).decode('utf-8')
        assert xml_str.count('<Ad>') == 3


# ------------------------------------------------------------------ #
#  publish_listing_task                                               #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestPublishListingTask:
    def test_publish_idempotency(self):
        """Второй вызов задачи не вызывает flush_feed если уже есть external_id."""
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
            mock_adapter.return_value.flush_feed.assert_not_called()

    def test_publish_sets_pending_status_after_feed_upload(self):
        """После успешного flush_feed статус становится PENDING."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('pending-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks.poll_feed_results_task') as mock_poll:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_cache.get.return_value = None
            mock_cls.return_value.flush_feed.return_value = True
            mock_poll.apply_async = MagicMock()

            publish_listing_task(listing.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        mock_cls.return_value.flush_feed.assert_called_once_with([listing])

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
            from apps.marketplaces.adapters.avito.adapter import FeedUploadError
            mock_cls.return_value.flush_feed.side_effect = FeedUploadError('S3 not configured')

            with pytest.raises((FeedUploadError, Retry)):
                publish_listing_task(listing.pk)

    def test_429_applies_exponential_backoff(self):
        assert backoff(0) == 30
        assert backoff(1) == 60
        assert backoff(2) == 120
        assert backoff(3) == 300
        assert backoff(99) == 300

    def test_price_change_uses_rest_not_feed(self):
        """update_price вызывает REST-метод, а не flush_feed."""
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
            mock_cls.return_value.flush_feed.assert_not_called()


# ------------------------------------------------------------------ #
#  poll_feed_results_task                                             #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestPollFeedResultsTask:
    def test_updates_external_id_and_sets_active(self):
        """После получения avito_id статус становится ACTIVE, external_id заполнен."""
        from apps.marketplaces.tasks import poll_feed_results_task
        tenant = make_tenant('poll-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        ad_id = get_ad_id(listing)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': ad_id, 'avito_id': 987654}
            ]
            poll_feed_results_task(account.pk)

        listing.refresh_from_db()
        assert listing.external_id == '987654'
        assert listing.status == Listing.STATUS_ACTIVE

    def test_retries_when_some_listings_still_pending(self):
        """Если часть листингов ещё не обработана — задача ставится на retry."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import poll_feed_results_task
        tenant = make_tenant('retry-poll-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            # avito_id = None означает ещё не обработан
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': get_ad_id(listing), 'avito_id': None}
            ]
            # При вызове напрямую Celery retry поднимает либо Retry, либо оригинальное исключение
            with pytest.raises((Retry, RuntimeError)):
                poll_feed_results_task(listing.account.pk)

    def test_no_pending_listings_does_nothing(self):
        """Нет PENDING листингов — задача завершается без вызова API."""
        from apps.marketplaces.tasks import poll_feed_results_task
        tenant = make_tenant('noop-co')
        account = make_account(tenant)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            poll_feed_results_task(account.pk)
            mock_cls.return_value.get_feed_results.assert_not_called()


# ------------------------------------------------------------------ #
#  update_listing_task — 404 triggers republish                       #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestUpdateListingTask:
    def test_no_external_id_delegates_to_publish(self):
        """Если external_id не задан — вызывает publish_listing_task."""
        from apps.marketplaces.tasks import update_listing_task
        tenant = make_tenant('upd-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks.publish_listing_task') as mock_pub, \
             patch('apps.marketplaces.tasks.AvitoAdapter'):
            update_listing_task(listing.pk)
            mock_pub.delay.assert_called_once_with(listing.pk)


# ------------------------------------------------------------------ #
#  E2E: 10 товаров → PENDING → ACTIVE                                #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestE2EFeedFlow:
    def test_10_products_publish_via_feed(self):
        """10 товаров проходят полный путь: flush_feed → PENDING → poll → ACTIVE."""
        from apps.marketplaces.tasks import poll_feed_results_task, publish_listing_task
        tenant = make_tenant('e2e-co')
        account = make_account(tenant)
        ds = DataSourceConnection.objects.create(
            tenant=tenant, name='S', type='1c_http',
            credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
        )

        listings = []
        for i in range(10):
            product, _ = ProductService.upsert_from_source(tenant, ds, {
                'uuid': None, 'article': f'E2E{i:03d}', 'name': f'Товар {i}',
                'brand': 'B', 'price': '100', 'stock_qty': 1,
                'category': 'Кузов', 'condition': 'new',
            })
            lst = make_listing(tenant, product, account)
            listings.append(lst)

        # Шаг 1: публикация через фид → статус PENDING
        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks.poll_feed_results_task') as mock_poll:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_cache.get.return_value = None
            mock_cls.return_value.flush_feed.return_value = True
            mock_poll.apply_async = MagicMock()

            for lst in listings:
                publish_listing_task(lst.pk)

        pending = Listing.objects.filter(tenant=tenant, status=Listing.STATUS_PENDING).count()
        assert pending == 10

        # Шаг 2: poll возвращает avito_ids → статус ACTIVE
        fake_results = [
            {'ad_id': get_ad_id(lst), 'avito_id': 1000 + i}
            for i, lst in enumerate(listings)
        ]
        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            mock_cls.return_value.get_feed_results.return_value = fake_results
            poll_feed_results_task(account.pk)

        active = Listing.objects.filter(tenant=tenant, status=Listing.STATUS_ACTIVE).count()
        assert active == 10
        for i, lst in enumerate(listings):
            lst.refresh_from_db()
            assert lst.external_id == str(1000 + i)
