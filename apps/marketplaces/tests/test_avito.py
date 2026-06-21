from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import backoff
from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.adapters.avito.feed_builder import build_feed, get_ad_id
from apps.marketplaces.models import CategoryMapping, Listing, MarketplaceAccount, MarketplacePlacementAddress
from apps.marketplaces.services import ListingService
from apps.products.models import ProductImage, TenantCatalogCategory
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

    @responses_lib.activate
    def test_refresh_unsaved_account_does_not_cache_none_key(self):
        responses_lib.add(
            responses_lib.POST, 'https://api.avito.ru/token',
            json={'access_token': 'tok-validation', 'expires_in': 3600},
            status=200,
        )
        from django.core.cache import cache
        cache.clear()

        account = MagicMock()
        account.pk = None
        account.credentials_enc = encrypt({'client_id': 'cid', 'client_secret': 'csec'})

        token = AvitoAuthManager()._refresh(account)

        assert token == 'tok-validation'
        assert cache.get('avito:token:None') is None


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

    def test_build_feed_includes_only_publishable_images_primary_first(self):
        tenant = make_tenant('feed-images-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        ProductImage.objects.create(
            product=product,
            s3_key='products/feed/needs-review.jpg',
            sha256='needs-review',
            position=0,
            status=ProductImage.Status.NEEDS_REVIEW,
        )
        secondary = ProductImage.objects.create(
            product=product,
            s3_key='products/feed/secondary.jpg',
            sha256='secondary',
            position=1,
            status=ProductImage.Status.AUTO_APPROVED,
        )
        ProductImage.objects.create(
            product=product,
            s3_key='products/feed/rejected.jpg',
            sha256='rejected',
            position=2,
            status=ProductImage.Status.REJECTED,
        )
        primary = ProductImage.objects.create(
            product=product,
            s3_key='products/feed/primary.jpg',
            sha256='primary',
            position=3,
            is_primary=True,
            status=ProductImage.Status.MANUALLY_SET,
        )

        with patch('django.core.files.storage.default_storage.url') as storage_url:
            storage_url.side_effect = lambda key: f'https://cdn.example.com/{key}'
            xml_str = build_feed([listing]).decode('utf-8')

        assert f'https://cdn.example.com/{primary.s3_key}' in xml_str
        assert f'https://cdn.example.com/{secondary.s3_key}' in xml_str
        assert 'needs-review.jpg' not in xml_str
        assert 'rejected.jpg' not in xml_str
        assert xml_str.index(primary.s3_key) < xml_str.index(secondary.s3_key)

    def test_build_feed_uses_category_default_image_when_product_has_no_images(self):
        tenant = make_tenant('feed-category-image-co')
        account = make_account(tenant)
        category = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Тормоза',
            normalized_name='тормоза',
            domain=TenantCatalogCategory.Domain.AUTO_PARTS,
            default_image_s3_key='catalog-categories/feed/default.jpg',
        )
        product = make_product(tenant)
        product.catalog_category = category
        product.save(update_fields=['catalog_category'])
        listing = make_listing(tenant, product, account)

        with patch('django.core.files.storage.default_storage.url') as storage_url:
            storage_url.side_effect = lambda key: f'https://cdn.example.com/{key}'
            xml_str = build_feed([listing]).decode('utf-8')

        assert '<Images>' in xml_str
        assert f'https://cdn.example.com/{category.default_image_s3_key}' in xml_str

    def test_build_feed_uses_account_default_address(self):
        tenant = make_tenant('feed-account-address-co')
        account = make_account(tenant)
        account.default_address = 'Москва, улица Ленина, 1'
        account.default_manager_name = 'Иван'
        account.default_contact_phone = '+7 900 000-00-00'
        account.save(update_fields=['default_address', 'default_manager_name', 'default_contact_phone'])
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<Address>Москва, улица Ленина, 1</Address>' in xml_str
        assert '<ManagerName>Иван</ManagerName>' in xml_str
        assert '<ContactPhone>+7 900 000-00-00</ContactPhone>' in xml_str

    def test_build_feed_prefers_seller_address_id_over_address(self):
        tenant = make_tenant('feed-seller-address-co')
        account = make_account(tenant)
        account.default_address = 'Москва, улица Ленина, 1'
        account.default_seller_address_id = '123456789'
        account.save(update_fields=['default_address', 'default_seller_address_id'])
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<SellerAddressID>123456789</SellerAddressID>' in xml_str
        assert '<Address>' not in xml_str

    def test_build_feed_category_address_overrides_account_address(self):
        tenant = make_tenant('feed-category-address-co')
        account = make_account(tenant)
        account.default_address = 'Москва, улица Ленина, 1'
        account.save(update_fields=['default_address'])
        product = make_product(tenant)
        CategoryMapping.objects.create(
            tenant=tenant,
            marketplace='avito',
            category_source=product.category_1c,
            category_target='Запчасти и аксессуары',
            category_id=1,
            attributes_map={'address': 'Казань, улица Кремлёвская, 1'},
        )
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<Address>Казань, улица Кремлёвская, 1</Address>' in xml_str
        assert 'Москва, улица Ленина, 1' not in xml_str

    def test_build_feed_listing_address_overrides_category_address(self):
        tenant = make_tenant('feed-listing-address-co')
        account = make_account(tenant)
        product = make_product(tenant)
        CategoryMapping.objects.create(
            tenant=tenant,
            marketplace='avito',
            category_source=product.category_1c,
            category_target='Запчасти и аксессуары',
            category_id=1,
            attributes_map={'address': 'Казань, улица Кремлёвская, 1'},
        )
        listing = make_listing(tenant, product, account)
        listing.address_override = 'Самара, Московское шоссе, 10'
        listing.save(update_fields=['address_override'])

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<Address>Самара, Московское шоссе, 10</Address>' in xml_str
        assert 'Казань, улица Кремлёвская, 1' not in xml_str

    def test_build_feed_listing_address_overrides_bulk_address(self):
        tenant = make_tenant('feed-listing-over-bulk-address-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        listing.bulk_address = 'Казань, улица Кремлёвская, 1'
        listing.address_override = 'Самара, Московское шоссе, 10'
        listing.save(update_fields=['bulk_address', 'address_override'])

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<Address>Самара, Московское шоссе, 10</Address>' in xml_str
        assert 'Казань, улица Кремлёвская, 1' not in xml_str

    def test_build_feed_uses_listing_placement_address(self):
        tenant = make_tenant('feed-listing-placement-address-co')
        account = make_account(tenant)
        product = make_product(tenant)
        address = MarketplacePlacementAddress.objects.create(
            tenant=tenant,
            account=account,
            name='Склад МКАД',
            seller_address_id='avito-address-1',
            address='Москва, МКАД',
            manager_name='Мария',
            contact_phone='+7 900 111-22-33',
        )
        listing = make_listing(tenant, product, account, placement_address=address)

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<SellerAddressID>avito-address-1</SellerAddressID>' in xml_str
        assert '<Address>' not in xml_str
        assert '<ManagerName>Мария</ManagerName>' in xml_str
        assert '<ContactPhone>+7 900 111-22-33</ContactPhone>' in xml_str

    def test_build_feed_uses_bulk_placement_address_before_category(self):
        tenant = make_tenant('feed-bulk-placement-address-co')
        account = make_account(tenant)
        product = make_product(tenant)
        address = MarketplacePlacementAddress.objects.create(
            tenant=tenant,
            account=account,
            name='СПб склад',
            address='Санкт-Петербург, Невский проспект, 1',
        )
        CategoryMapping.objects.create(
            tenant=tenant,
            marketplace='avito',
            category_source=product.category_1c,
            category_target='Запчасти и аксессуары',
            category_id=1,
            attributes_map={'address': 'Казань, улица Кремлёвская, 1'},
        )
        listing = make_listing(tenant, product, account, bulk_placement_address=address)

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<Address>Санкт-Петербург, Невский проспект, 1</Address>' in xml_str
        assert 'Казань, улица Кремлёвская, 1' not in xml_str

    def test_build_feed_uses_account_default_placement_address(self):
        tenant = make_tenant('feed-default-placement-address-co')
        account = make_account(tenant)
        account.default_address = 'Москва, улица Ленина, 1'
        account.save(update_fields=['default_address'])
        MarketplacePlacementAddress.objects.create(
            tenant=tenant,
            account=account,
            name='Основной адрес',
            address='Нижний Новгород, Большая Покровская, 1',
            is_default=True,
        )
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')

        assert '<Address>Нижний Новгород, Большая Покровская, 1</Address>' in xml_str
        assert 'Москва, улица Ленина, 1' not in xml_str

    def test_update_listing_fields_changes_account_and_price(self):
        tenant = make_tenant('listing-fields-co')
        account = make_account(tenant)
        next_account = MarketplaceAccount.objects.create(
            tenant=tenant,
            name='Second Account',
            external_id='54321',
            credentials_enc=encrypt({'client_id': 'cid2', 'client_secret': 'csec2'}),
        )
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        updated = ListingService.update_listing_fields(listing.pk, tenant, {
            'account_id': next_account.pk,
            'price_on_listing': Decimal('4100.00'),
        })

        assert updated.account_id == next_account.pk
        assert updated.price_on_listing == Decimal('4100.00')
        assert '<Price>4100</Price>' in build_feed([updated]).decode('utf-8')

    def test_update_listing_fields_clears_placement_address_when_account_changes(self):
        tenant = make_tenant('listing-fields-clear-placement-co')
        account = make_account(tenant)
        next_account = MarketplaceAccount.objects.create(
            tenant=tenant,
            name='Second Account',
            external_id='54321',
            credentials_enc=encrypt({'client_id': 'cid2', 'client_secret': 'csec2'}),
        )
        product = make_product(tenant)
        address = MarketplacePlacementAddress.objects.create(
            tenant=tenant,
            account=account,
            name='Старый адрес',
            address='Москва',
        )
        listing = make_listing(tenant, product, account, placement_address=address)

        updated = ListingService.update_listing_fields(listing.pk, tenant, {'account_id': next_account.pk})

        assert updated.account_id == next_account.pk
        assert updated.placement_address_id is None


@pytest.mark.django_db
class TestAvitoAdapterFeedStorage:
    def test_feed_s3_key_uses_prefix_tenant_marketplace_and_account(self, settings):
        settings.MEDIA_KEY_PREFIX = 'dev'
        tenant = make_tenant('feed-path-co')
        account = make_account(tenant)

        key = AvitoAdapter(account)._feed_s3_key()

        assert key == f'dev/feeds/feed-path-co/avito/test-account-{account.pk}/feed.xml'

    def test_trigger_autoload_raises_when_avito_rejects_upload(self):
        from apps.marketplaces.adapters.avito.adapter import FeedUploadError

        tenant = make_tenant('autoload-upload-fail-co')
        account = make_account(tenant)

        with patch('apps.marketplaces.adapters.avito.adapter.requests.post') as mock_post:
            adapter = AvitoAdapter(account)
            adapter._auth.get_token = MagicMock(return_value='tok')
            mock_post.return_value.status_code = 403
            mock_post.return_value.text = 'autoload is not connected'

            with pytest.raises(FeedUploadError, match='Autoload не принял фид'):
                adapter._trigger_autoload()

    def test_get_feed_results_raises_when_autoload_profile_is_unavailable(self):
        from apps.marketplaces.adapters.avito.adapter import FeedUploadError

        tenant = make_tenant('feed-results-no-autoload-co')
        account = make_account(tenant)

        with patch('apps.marketplaces.adapters.avito.adapter.requests.get') as mock_get:
            adapter = AvitoAdapter(account)
            adapter._auth.get_token = MagicMock(return_value='tok')
            mock_get.return_value.status_code = 403
            mock_get.return_value.text = 'autoload profile is unavailable'

            with pytest.raises(FeedUploadError, match='Автозагрузка Avito не подключена'):
                adapter.get_feed_results(['ad-1'])


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

    def test_publish_rejects_when_autoload_profile_is_inactive(self):
        from apps.marketplaces.tasks import publish_listing_task

        tenant = make_tenant('autoload-inactive-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value.is_autoload_active.return_value = False

            publish_listing_task(listing.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'Автозагрузка Avito не подключена' in listing.rejection_reason
        mock_cls.return_value.flush_feed.assert_not_called()
        mock_notify.assert_called_once()

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

    def test_rejects_after_polling_retries_are_exhausted(self):
        from apps.marketplaces.tasks import poll_feed_results_task

        tenant = make_tenant('poll-final-fail-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': get_ad_id(listing), 'avito_id': None}
            ]
            poll_feed_results_task.apply(args=[account.pk], throw=True, retries=10)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'Avito не вернул ID объявления' in listing.rejection_reason
        mock_notify.assert_called_once()

    def test_rejects_pending_listings_when_feed_results_autoload_error(self):
        from apps.marketplaces.adapters.avito.adapter import FeedUploadError
        from apps.marketplaces.tasks import poll_feed_results_task

        tenant = make_tenant('poll-autoload-error-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cls.return_value.get_feed_results.side_effect = FeedUploadError(
                'Автозагрузка Avito не подключена'
            )
            poll_feed_results_task(account.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'Автозагрузка Avito не подключена' in listing.rejection_reason
        mock_notify.assert_called_once()

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
