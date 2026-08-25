from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest
import requests
import responses as responses_lib
from django.core.cache.backends.locmem import LocMemCache
from django.utils.timezone import now

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import backoff
from apps.marketplaces.adapters.avito.adapter import (
    AmbiguousFeedSubmissionError,
    AvitoAdapter,
    FeedUploadError,
    _avito_request,
)
from apps.marketplaces.adapters.avito.feed_builder import build_feed, get_ad_id
from apps.marketplaces.adapters.avito import rate_limiter
from apps.marketplaces.adapters.avito.rate_limiter import (
    AvitoRateLimiter,
    RateLimitError,
)
from apps.marketplaces.models import CategoryMapping, Listing, MarketplaceAccount, MarketplacePlacementAddress
from apps.marketplaces.services import ListingBulkLimitExceeded, ListingService
from apps.products.models import Product, ProductImage, TenantCatalogCategory
from apps.products.services import ProductService
from apps.sync.models import SyncLog
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
        default_manager_name='Менеджер',
        default_contact_phone='+79990000000',
    )


def make_product(tenant):
    ds = DataSourceConnection.objects.create(
        tenant=tenant, name='S', type='1c_http',
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    product, _, _ = ProductService.upsert_from_source(tenant, ds, {
        'uuid': None, 'article': 'ART-001', 'name': 'Тормозной диск',
        'brand': 'Bosch', 'price': '3500', 'stock_qty': 5,
        'category': 'Тормоза', 'condition': 'new',
    })
    return product


def make_product_with_article(tenant, article):
    product = make_product(tenant)
    return Product.objects.create(
        tenant=tenant,
        datasource=product.datasource,
        article=article,
        name=f'Тестовый товар {article}',
        brand='Bosch',
        price='3500',
        stock_qty=5,
        category_1c='Тормоза',
        condition='new',
    )


def make_listing(tenant, product, account, **kwargs):
    kwargs.setdefault('status', Listing.STATUS_DRAFT)
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        price_on_listing=Decimal('3500'),
        title='Тормозной диск Bosch',
        description_ai='Описание тестовое',
        **kwargs,
    )


def assert_exact_feed_flush_scheduled(mock_flush, account):
    """Assert the P5 coordinator published one exact, repairable revision."""

    mock_flush.apply_async.assert_called_once()
    kwargs = mock_flush.apply_async.call_args.kwargs
    account.refresh_from_db()
    assert kwargs['args'][0] == account.pk
    assert kwargs['args'][1] == account.feed_intent_revision
    assert isinstance(kwargs['args'][2], str) and kwargs['args'][2]
    assert kwargs['expires'] < kwargs['countdown'] + 300


def test_rate_limiter_creates_ttl_window_and_rejects_only_over_limit(monkeypatch):
    local_cache = LocMemCache('avito-rate-limit-test', {})
    monkeypatch.setattr(rate_limiter, 'cache', local_cache)
    monkeypatch.setitem(rate_limiter.RATE_LIMITS, 'test-operation', {
        'rate': 2,
        'per': 60,
    })
    account = SimpleNamespace(pk=42)
    limiter = AvitoRateLimiter()

    with patch.object(limiter, '_log_rate_limit') as log_rate_limit:
        limiter.consume(account, 'test-operation')
        limiter.consume(account, 'test-operation')
        with pytest.raises(RateLimitError) as exc_info:
            limiter.consume(account, 'test-operation')

    assert exc_info.value.retry_after == 60
    assert local_cache.get('avito:rl:42:test-operation') == 3
    log_rate_limit.assert_called_once_with(account, 'test-operation')


def test_avito_request_emits_bounded_5xx_telemetry():
    response = MagicMock(status_code=503)
    requester = MagicMock()

    with (
        patch(
            'apps.marketplaces.adapters.avito.adapter.bounded_http_request',
            return_value=response,
        ),
        patch('apps.marketplaces.adapters.avito.adapter.metric_count') as count,
        patch(
            'apps.marketplaces.adapters.avito.adapter.metric_distribution',
        ) as distribution,
    ):
        result = _avito_request(
            requester,
            'https://api.avito.ru/test',
            operation='status',
        )

    assert result is response
    count.assert_called_once_with(
        'map.provider.request',
        attributes={
            'provider': 'avito',
            'operation': 'status',
            'outcome': 'failure',
            'response_class': '5xx',
        },
    )
    assert distribution.call_args.kwargs['attributes']['response_class'] == '5xx'


def test_avito_request_emits_remote_429_and_network_error_telemetry():
    response = MagicMock(status_code=429)
    with (
        patch(
            'apps.marketplaces.adapters.avito.adapter.bounded_http_request',
            return_value=response,
        ),
        patch('apps.marketplaces.adapters.avito.adapter.metric_count') as count,
        patch('apps.marketplaces.adapters.avito.adapter.metric_distribution'),
    ):
        _avito_request(MagicMock(), 'https://api.avito.ru/test', operation='price')

    assert count.call_args_list[0].kwargs['attributes'] == {
        'provider': 'avito',
        'operation': 'price',
        'rate_limit_source': 'remote',
    }
    assert count.call_args_list[1].kwargs['attributes']['response_class'] == '4xx'

    with (
        patch(
            'apps.marketplaces.adapters.avito.adapter.bounded_http_request',
            side_effect=TimeoutError('provider timeout'),
        ),
        patch('apps.marketplaces.adapters.avito.adapter.metric_count') as count,
        patch('apps.marketplaces.adapters.avito.adapter.metric_distribution'),
        pytest.raises(TimeoutError),
    ):
        _avito_request(MagicMock(), 'https://api.avito.ru/test', operation='price')

    assert count.call_args.kwargs['attributes']['response_class'] == 'network_error'


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

    @pytest.mark.parametrize(
        'payload',
        [
            [],
            {'access_token': '', 'expires_in': 3600},
            {'access_token': 'token', 'expires_in': True},
            {'access_token': 'token', 'expires_in': -1},
            {'access_token': 'x' * 8193, 'expires_in': 3600},
        ],
    )
    @responses_lib.activate
    def test_refresh_rejects_malformed_token_response(self, payload):
        responses_lib.add(
            responses_lib.POST,
            'https://api.avito.ru/token',
            json=payload,
            status=200,
        )
        account = MagicMock()
        account.pk = None
        account.credentials_enc = encrypt({
            'client_id': 'cid',
            'client_secret': 'csec',
        })

        with pytest.raises(ValueError, match='некоррект'):
            AvitoAuthManager()._refresh(account)


class TestAvitoAdapterNetworkBounds:
    @staticmethod
    def _adapter():
        adapter = object.__new__(AvitoAdapter)
        adapter.account = MagicMock()
        adapter._auth = MagicMock()
        adapter._auth.get_token.return_value = 'token'
        return adapter

    def test_latest_upload_strict_keeps_empty_success_authoritative(self):
        response = MagicMock(ok=True)
        response.json.return_value = {'uploads': []}

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ):
            result = self._adapter().get_latest_upload(strict=True)

        assert result == {}

    def test_latest_upload_strict_propagates_network_and_json_failures(self):
        adapter = self._adapter()
        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            side_effect=requests.Timeout('provider timeout'),
        ):
            with pytest.raises(requests.Timeout, match='provider timeout'):
                adapter.get_latest_upload(strict=True)

        malformed = MagicMock(ok=True)
        malformed.json.return_value = []
        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=malformed,
        ):
            with pytest.raises(ValueError, match='top-level JSON'):
                adapter.get_latest_upload(strict=True)

    def test_latest_upload_legacy_mode_swallows_network_failure(self):
        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            side_effect=requests.Timeout('legacy provider timeout'),
        ):
            assert self._adapter().get_latest_upload() == {}

    def test_feed_item_error_pagination_stops_at_hard_page_limit(self, settings):
        settings.AVITO_API_MAX_PAGES = 2

        def response_for_page(*_args, **kwargs):
            response = MagicMock(ok=True)
            response.json.return_value = {
                'items': [],
                'meta': {'pages': 999},
            }
            return response

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            side_effect=response_for_page,
        ) as request:
            result = self._adapter().get_feed_item_errors(['wanted-id'])

        assert result == {}
        assert request.call_count == 2
        assert [call.kwargs['params']['page'] for call in request.call_args_list] == [1, 2]

    def test_feed_item_error_pagination_ends_safely_on_malformed_json(self):
        response = MagicMock(ok=True)
        response.json.return_value = []

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ) as request:
            result = self._adapter().get_feed_item_errors(['wanted-id'])

        assert result == {}
        request.assert_called_once()


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

    def test_build_feed_enforces_avito_text_limits(self):
        tenant = make_tenant('feed-text-limits-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)
        # update() bypasses the model field's max_length and represents legacy data.
        Listing.objects.filter(pk=listing.pk).update(
            title='Заголовок ' * 28,
            description_ai='Описание товара ' * 700,
        )
        listing.refresh_from_db()

        root = ET.fromstring(build_feed([listing]))

        assert len(root.findtext('./Ad/Title')) <= 200
        assert len(root.findtext('./Ad/Description')) <= 7500

    def test_build_feed_includes_ad_type_and_goods_type(self):
        """Фид содержит обязательные для категории теги AdType и GoodsType."""
        tenant = make_tenant('feed-adtype-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, ad_type=Listing.AD_TYPE_RESALE)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<AdType>Товар приобретен на продажу</AdType>' in xml_str
        assert '<GoodsType>Запчасти</GoodsType>' in xml_str

    def test_build_feed_ad_type_defaults_to_resale(self):
        """Без явного выбора AdType по умолчанию «Товар приобретен на продажу»."""
        tenant = make_tenant('feed-adtype-def-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<AdType>Товар приобретен на продажу</AdType>' in xml_str

    def test_build_feed_product_type_from_category_mapping(self):
        """ProductType («Тип товара») берётся из attributes_map маппинга категории."""
        tenant = make_tenant('feed-producttype-co')
        account = make_account(tenant)
        product = make_product(tenant)
        CategoryMapping.objects.create(
            tenant=tenant,
            marketplace='avito',
            category_source=product.category_1c,
            category_target='Запчасти и аксессуары',
            category_id=1,
            attributes_map={'GoodsType': 'Запчасти', 'ProductType': 'Для автомобилей'},
        )
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<GoodsType>Запчасти</GoodsType>' in xml_str
        assert '<ProductType>Для автомобилей</ProductType>' in xml_str

    def test_build_feed_omits_product_type_when_not_configured(self):
        """Без маппинга ProductType в фид не попадает (нет дефолта)."""
        tenant = make_tenant('feed-no-producttype-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<ProductType>' not in xml_str

    def test_build_feed_includes_brand_and_oem_fallback_to_article(self):
        """Brand берётся из товара; без OEM в <OEM> подставляется артикул."""
        tenant = make_tenant('feed-brand-oem-co')
        account = make_account(tenant)
        product = make_product(tenant)  # brand=Bosch, article=ART-001, без oem
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<Brand>Bosch</Brand>' in xml_str
        assert '<OEM>ART-001</OEM>' in xml_str

    def test_build_feed_oem_uses_oem_numbers_when_present(self):
        """При наличии oem_numbers в <OEM> идут они, а не артикул."""
        tenant = make_tenant('feed-oem-real-co')
        account = make_account(tenant)
        product = make_product(tenant)
        product.oem_numbers = ['92101-1234', '92102-5678']
        product.save(update_fields=['oem_numbers'])
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '92101-1234' in xml_str and '92102-5678' in xml_str
        assert 'ART-001' not in xml_str.split('<OEM>')[1].split('</OEM>')[0]

    def test_build_feed_brand_falls_back_to_tenant_name(self):
        """Без бренда у товара Brand = название организации тенанта."""
        tenant = make_tenant('feed-brand-fallback-co')
        account = make_account(tenant)
        product = make_product(tenant)
        product.brand = ''
        product.save(update_fields=['brand'])
        listing = make_listing(tenant, product, account)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<Brand>feed-brand-fallback-co</Brand>' in xml_str

    def test_build_feed_ignores_account_id_used_as_seller_address(self):
        """external_id аккаунта в SellerAddressID игнорируется — шлём текстовый адрес."""
        tenant = make_tenant('feed-bad-sid-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(
            tenant, product, account,
            seller_address_id_override=account.external_id,
            address_override='Москва, Тверская, 1',
        )

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<SellerAddressID>' not in xml_str
        assert '<Address>Москва, Тверская, 1</Address>' in xml_str

    def test_build_feed_never_emits_status_remove(self):
        """build_feed не использует выдуманный <Status>Remove</Status> (его в формате Avito нет)."""
        tenant = make_tenant('feed-rm-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_ARCHIVED)

        xml_str = build_feed([listing]).decode('utf-8')
        assert '<Status>' not in xml_str

    def test_build_stop_feed_is_stop_command(self):
        """build_stop_feed возвращает документированную команду снятия всех объявлений."""
        from apps.marketplaces.adapters.avito.feed_builder import build_stop_feed
        xml_str = build_stop_feed().decode('utf-8')
        assert '<Id>STOP</Id>' in xml_str
        assert 'formatVersion="3"' in xml_str

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
            product, _, _ = ProductService.upsert_from_source(tenant, ds, {
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
        tenant = make_tenant('autoload-upload-fail-co')
        account = make_account(tenant)

        with patch('apps.marketplaces.adapters.avito.adapter.requests.post') as mock_post:
            adapter = AvitoAdapter(account)
            adapter._auth.get_token = MagicMock(return_value='tok')
            mock_post.return_value.status_code = 403
            mock_post.return_value.text = 'autoload is not connected'

            with pytest.raises(FeedUploadError, match='Autoload не принял фид'):
                adapter._trigger_autoload()

    @pytest.mark.parametrize('status_code', (400, 401, 403, 404, 422))
    def test_trigger_autoload_treats_known_client_rejections_as_safe(
        self,
        status_code,
    ):
        tenant = make_tenant(f'autoload-safe-rejection-{status_code}-co')
        account = make_account(tenant)
        response = MagicMock(status_code=status_code)

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ):
            adapter = AvitoAdapter(account)
            adapter._auth.get_token = MagicMock(return_value='tok')

            with pytest.raises(FeedUploadError) as error:
                adapter._trigger_autoload()

        assert type(error.value) is FeedUploadError

    @pytest.mark.parametrize('status_code', (408, 425, 500, 502, 503, 504))
    def test_trigger_autoload_marks_unknown_provider_outcome_ambiguous(
        self,
        status_code,
    ):
        tenant = make_tenant(f'autoload-ambiguous-{status_code}-co')
        account = make_account(tenant)
        response = MagicMock(status_code=status_code)

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ):
            adapter = AvitoAdapter(account)
            adapter._auth.get_token = MagicMock(return_value='tok')

            with pytest.raises(
                AmbiguousFeedSubmissionError,
                match='неоднозначный ответ',
            ):
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
    def test_publish_product_creates_draft_without_autopublish(self):
        """Со страницы товаров создаётся ЧЕРНОВИК — без авто-отправки в Avito."""
        from apps.marketplaces.services import ListingService
        tenant = make_tenant('prod-draft-co')
        make_account(tenant)
        product = make_product(tenant)

        with patch('apps.marketplaces.services.transaction') as mock_tx:
            ids = ListingService.publish_product(product, tenant)

        listing = Listing.objects.get(pk=ids[0])
        assert listing.status == Listing.STATUS_DRAFT
        # auto_publish=False → никаких on_commit (ни публикации, ни апдейта)
        mock_tx.on_commit.assert_not_called()

    def test_republish_from_archive_clears_external_id_and_publishes(self):
        """Перепубликация из архива: stale external_id сбрасывается, фид уходит (не висит в очереди)."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('republish-arch-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(
            tenant, product, account,
            status=Listing.STATUS_QUEUED, external_id='OLD-AVITO-ID',
        )

        with patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks._validate_feed_batch', side_effect=lambda rows: rows), \
             patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            publish_listing_task(listing.pk)

        listing.refresh_from_db()
        assert listing.external_id is None
        assert listing.status == Listing.STATUS_PENDING
        assert_exact_feed_flush_scheduled(mock_flush, account)

    def test_publish_idempotency(self):
        """Активное объявление (external_id + status=active) повторно не публикуется."""
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

    def test_publish_sets_pending_and_requests_flush(self):
        """Публикация помечает «На модерации Авито» и отдаёт фид координатору окна."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('pending-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks._validate_feed_batch', side_effect=lambda rows: rows), \
             patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            publish_listing_task(listing.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        assert_exact_feed_flush_scheduled(mock_flush, account)

    def test_publish_rejects_listing_without_contacts(self):
        """Без контактного лица/телефона объявление отклоняется и не уходит в feed."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('no-contacts-co')
        account = make_account(tenant)
        account.default_manager_name = ''
        account.default_contact_phone = ''
        account.save(update_fields=['default_manager_name', 'default_contact_phone'])
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks._notify_error'):
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            mock_cache.get.return_value = None

            publish_listing_task(listing.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'контактное лицо' in listing.rejection_reason.lower()
        mock_cls.return_value.flush_feed.assert_not_called()

    def test_publish_rejects_new_product_without_brand(self):
        """Новая запчасть без производителя отклоняется сразу с понятной причиной —
        Avito валидирует Brand по каталогу, фолбэк на имя тенанта не проходит."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('brand-block-co')
        account = make_account(tenant)
        product = make_product(tenant)
        product.brand = ''
        product.save(update_fields=['brand'])
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks._notify_error'):
            result = _validate_feed_batch([listing])

        assert result == []
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'производитель' in listing.rejection_reason.lower()
        assert 'карточке товара' in listing.rejection_reason

    def test_publish_allows_used_product_without_brand(self):
        """Для б/у запчастей пустой бренд не блокирует публикацию."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('brand-used-co')
        account = make_account(tenant)
        product = make_product(tenant)
        product.brand = ''
        product.condition = 'used'
        product.save(update_fields=['brand', 'condition'])
        listing = make_listing(tenant, product, account)

        result = _validate_feed_batch([listing])

        assert result == [listing]

    def test_unknown_brand_goes_to_review_rest_of_batch_passes(self):
        """Бренд не из каталога Avito → листинг на проверку с понятной причиной,
        остальная партия массового постинга публикуется без задержки."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('brand-review-co')
        account = make_account(tenant)
        bad_product = make_product(tenant)
        bad_product.brand = 'НесуществующийБрендХYZ'
        bad_product.save(update_fields=['brand'])
        good_product = make_product_with_article(tenant, 'ART-GOOD')  # brand=Bosch
        bad = make_listing(tenant, bad_product, account)
        good = make_listing(tenant, good_product, account)

        with patch('apps.marketplaces.tasks._notify_error') as mock_notify, \
             patch('apps.marketplaces.adapters.avito.brand_catalog.catalog_status',
                   return_value={'stale': False}):
            result = _validate_feed_batch([bad, good])

        assert result == [good]
        bad.refresh_from_db()
        assert bad.status == Listing.STATUS_REQUIRES_REVIEW
        assert 'Avito не распознал производителя' in bad.rejection_reason
        assert 'Проверьте написание производителя' in bad.rejection_reason
        mock_notify.assert_called_once()

    def test_acknowledged_unknown_brand_remains_blocked(self):
        """Неизвестный бренд нельзя протолкнуть повторным одобрением."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('brand-ack-co')
        account = make_account(tenant)
        product = make_product(tenant)
        product.brand = 'НесуществующийБрендХYZ'
        product.save(update_fields=['brand'])
        listing = make_listing(tenant, product, account)
        listing.rejection_reason = (
            'Производителя «НесуществующийБрендХYZ» нет в каталоге Avito — далее старый текст.'
        )
        listing.save(update_fields=['rejection_reason'])

        with patch('apps.marketplaces.adapters.avito.brand_catalog.catalog_status',
                   return_value={'stale': False}), \
             patch('apps.marketplaces.tasks._notify_error'):
            result = _validate_feed_batch([listing])

        assert result == []
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REQUIRES_REVIEW

    def test_changed_brand_is_rechecked_after_review(self):
        """Тенант исправил бренд на другой, тоже неизвестный → новая проверка
        (маркер в причине относится к старому бренду)."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('brand-recheck-co')
        account = make_account(tenant)
        product = make_product(tenant)
        product.brand = 'ДругойНеизвестныйБренд'
        product.save(update_fields=['brand'])
        listing = make_listing(tenant, product, account)
        listing.rejection_reason = 'Производителя «НесуществующийБрендХYZ» нет в каталоге Avito — старое.'
        listing.save(update_fields=['rejection_reason'])

        with patch('apps.marketplaces.tasks._notify_error'), \
             patch('apps.marketplaces.adapters.avito.brand_catalog.catalog_status',
                   return_value={'stale': False}):
            result = _validate_feed_batch([listing])

        assert result == []
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REQUIRES_REVIEW
        assert 'ДругойНеизвестныйБренд' in listing.rejection_reason

    def test_publish_rejects_when_required_subtype_missing(self):
        """Категория требует под-вид (Подкатегория 3), он не выбран → отклоняем
        сразу с понятной причиной, а не постфактум из отчёта Avito."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('subtype-block-co')
        account = make_account(tenant)
        product = make_product(tenant)
        category = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Трансмиссия и привод',
            normalized_name='трансмиссияипривод',
            domain=TenantCatalogCategory.Domain.AUTO_PARTS,
            external_source='avito',
            external_id='transmissiia_i_privod',
        )
        product.catalog_category = category
        product.save(update_fields=['catalog_category'])
        listing = make_listing(tenant, product, account)

        with patch('apps.marketplaces.tasks._notify_error'):
            result = _validate_feed_batch([listing])

        assert result == []
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'Тип детали трансмиссии' in listing.rejection_reason
        assert 'Категория Avito' in listing.rejection_reason
        assert 'Подкатегорию 3' not in listing.rejection_reason

    def test_publish_passes_when_subtype_selected(self):
        """С выбранным под-видом (товар ниже листа) валидация пропускает объявление."""
        from apps.marketplaces.tasks import _validate_feed_batch
        tenant = make_tenant('subtype-ok-co')
        account = make_account(tenant)
        product = make_product(tenant)
        leaf = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Трансмиссия и привод',
            normalized_name='трансмиссияипривод',
            domain=TenantCatalogCategory.Domain.AUTO_PARTS,
            external_source='avito',
            external_id='transmissiia_i_privod',
        )
        subtype = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Крепёж КПП',
            normalized_name='крепёжкпп',
            domain=TenantCatalogCategory.Domain.AUTO_PARTS,
            external_source='avito',
            parent=leaf,
        )
        product.catalog_category = subtype
        product.save(update_fields=['catalog_category'])
        listing = make_listing(tenant, product, account)

        result = _validate_feed_batch([listing])

        assert result == [listing]

    def test_republish_from_limit_reached_after_subscription_renewal(self):
        """«Лимит достигнут» — не тупик: при активной подписке повторная публикация проходит."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('limit-recover-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_LIMIT_REACHED)

        with patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks._validate_feed_batch', side_effect=lambda rows: rows), \
             patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            publish_listing_task(listing.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        assert_exact_feed_flush_scheduled(mock_flush, account)

    def test_requeue_limit_reached_listings_targets_only_limit_reached(self):
        """Задача перезапуска трогает только листинги «Лимит достигнут» своего тенанта."""
        from apps.marketplaces.tasks import requeue_limit_reached_listings
        tenant = make_tenant('requeue-co')
        account = make_account(tenant)
        limited = make_listing(
            tenant, make_product(tenant), account, status=Listing.STATUS_LIMIT_REACHED,
        )
        make_listing(
            tenant, make_product_with_article(tenant, 'ART-ACT'), account,
            status=Listing.STATUS_ACTIVE,
        )

        with patch('apps.marketplaces.tasks.publish_listing_task') as mock_publish:
            result = requeue_limit_reached_listings(tenant.pk)

        assert result == {'requeued': 1}
        mock_publish.delay.assert_called_once_with(limited.pk)

    def test_listing_service_publish_allows_limit_reached(self):
        """ListingService.publish принимает limit_reached и переводит в очередь."""
        from apps.marketplaces.services import ListingService
        tenant = make_tenant('svc-limit-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_LIMIT_REACHED)

        with patch('apps.marketplaces.services.transaction'):
            result = ListingService.publish(listing.pk, tenant)

        assert result.status == Listing.STATUS_QUEUED

    def test_validate_warns_when_category_not_resolved(self):
        """Категория не определена → предупреждаем тенанта (но не блокируем)."""
        from apps.marketplaces.tasks import _validate_feed_batch
        from apps.sync.models import SyncLog
        tenant = make_tenant('cat-warn-co')
        account = make_account(tenant)
        listing = make_listing(tenant, make_product(tenant), account)

        result = _validate_feed_batch([listing])

        assert result == [listing]
        assert SyncLog.objects.filter(
            tenant=tenant, message__icontains='не определена категория',
        ).exists()

    def test_publish_does_not_serialize_behind_active_feed(self):
        """Новое объявление сразу «На модерации» (не висит «В очереди» за предыдущим фидом)."""
        from apps.marketplaces.tasks import publish_listing_task
        tenant = make_tenant('queue-waits-co')
        account = make_account(tenant)
        product = make_product(tenant)
        make_listing(tenant, product, account, status=Listing.STATUS_PENDING)
        queued_product = make_product_with_article(tenant, 'ART-002')
        queued = make_listing(tenant, queued_product, account, status=Listing.STATUS_QUEUED)

        with patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks._validate_feed_batch', side_effect=lambda rows: rows), \
             patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush:
            mock_cache.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache.lock.return_value.__exit__ = MagicMock(return_value=False)
            publish_listing_task(queued.pk)

        queued.refresh_from_db()
        assert queued.status == Listing.STATUS_PENDING
        assert_exact_feed_flush_scheduled(mock_flush, account)

    def test_unpublish_sets_archiving_and_requests_flush(self):
        """Снятие помечает «Снимается» и отдаёт фид координатору окна (без немедленного flush)."""
        from apps.marketplaces.tasks import unpublish_listing_task
        tenant = make_tenant('unpub-fullstate-co')
        account = make_account(tenant)
        make_listing(
            tenant, make_product(tenant), account,
            status=Listing.STATUS_ACTIVE, external_id='AV-ACTIVE-1',
        )
        target = make_listing(
            tenant, make_product_with_article(tenant, 'ART-2'), account,
            status=Listing.STATUS_ACTIVE, external_id='AV-TARGET-2',
        )

        with patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush:
            unpublish_listing_task(target.pk)

        target.refresh_from_db()
        assert target.status == Listing.STATUS_ARCHIVING
        assert_exact_feed_flush_scheduled(mock_flush, account)

    def test_unpublish_sets_archiving_not_archived(self):
        """Снятие переводит в «Снимается», а не сразу «В архиве» (ждём подтверждения)."""
        from apps.marketplaces.tasks import unpublish_listing_task
        tenant = make_tenant('unpub-archiving-co')
        account = make_account(tenant)
        listing = make_listing(
            tenant, make_product(tenant), account,
            status=Listing.STATUS_ACTIVE, external_id='AV-ARCHN-1',
        )
        with patch('apps.marketplaces.tasks.coalesced_flush_task'):
            unpublish_listing_task(listing.pk)
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_ARCHIVING

    def test_confirm_removal_archived_when_avito_removed(self):
        """confirm_removal_task: Avito больше не активно → «В архиве»."""
        from apps.marketplaces.tasks import confirm_removal_task
        tenant = make_tenant('confirm-rm-co')
        account = make_account(tenant)
        listing = make_listing(
            tenant, make_product(tenant), account,
            status=Listing.STATUS_ARCHIVING, external_id='AV-RM-1',
        )
        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            mock_cls.return_value.get_status.return_value = {'status': 'old'}
            confirm_removal_task(listing.pk)
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_ARCHIVED

    def test_confirm_removal_keeps_archiving_when_still_active(self):
        """confirm_removal_task: Avito ещё активно → остаётся «Снимается»."""
        from apps.marketplaces.tasks import confirm_removal_task
        tenant = make_tenant('confirm-rm2-co')
        account = make_account(tenant)
        listing = make_listing(
            tenant, make_product(tenant), account,
            status=Listing.STATUS_ARCHIVING, external_id='AV-RM-2',
        )
        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls:
            mock_cls.return_value.get_status.return_value = {'status': 'active'}
            confirm_removal_task(listing.pk)
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_ARCHIVING

    def test_coalesced_flush_rejects_when_autoload_profile_is_inactive(self):
        from apps.marketplaces.tasks import coalesced_flush_task

        tenant = make_tenant('autoload-inactive-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'), \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cls.return_value.is_autoload_active.return_value = False
            coalesced_flush_task(account.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert 'Автозагрузка Avito не подключена' in listing.rejection_reason
        mock_cls.return_value.flush_feed.assert_not_called()
        mock_notify.assert_called_once()

    def test_coalesced_flush_retries_on_feed_upload_error(self):
        """Сбой загрузки фида → задача уходит в retry (повтор позже)."""
        from celery.exceptions import Retry
        from apps.marketplaces.adapters.avito.adapter import FeedUploadError
        from apps.marketplaces.tasks import coalesced_flush_task
        tenant = make_tenant('token-co')
        account = make_account(tenant)
        product = make_product(tenant)
        make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'):
            mock_cls.return_value.is_autoload_active.return_value = True
            mock_cls.return_value.flush_feed.side_effect = FeedUploadError('S3 not configured')

            with pytest.raises((FeedUploadError, Retry)):
                coalesced_flush_task(account.pk)

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
#  Часовое окно автозагрузки: coalesced_flush_task + координатор       #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestCoalescedFlushTask:
    def test_promotes_queued_and_flushes_full_state(self):
        """Единый flush промотирует QUEUED→PENDING и грузит фид со всем состоянием."""
        from apps.marketplaces.tasks import coalesced_flush_task
        tenant = make_tenant('coalesce-batch-co')
        account = make_account(tenant)
        first = make_listing(tenant, make_product(tenant), account, status=Listing.STATUS_QUEUED)
        second = make_listing(
            tenant, make_product_with_article(tenant, 'ART-002'), account,
            status=Listing.STATUS_QUEUED,
        )

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'), \
             patch('apps.marketplaces.tasks.poll_feed_results_task') as mock_poll:
            mock_cls.return_value.is_autoload_active.return_value = True
            mock_poll.apply_async = MagicMock()
            coalesced_flush_task(account.pk)

        first.refresh_from_db()
        second.refresh_from_db()
        account.refresh_from_db()
        assert first.status == Listing.STATUS_PENDING
        assert second.status == Listing.STATUS_PENDING
        flushed = mock_cls.return_value.flush_feed.call_args[0][0]
        assert {item.pk for item in flushed} == {first.pk, second.pk}
        assert account.last_feed_flush_at is not None
        mock_poll.apply_async.assert_called_once()

    def test_excludes_archiving_listing_from_feed(self):
        """Снимаемое (ARCHIVING) объявление исключается из фида — Avito его архивирует."""
        from apps.marketplaces.tasks import coalesced_flush_task
        tenant = make_tenant('coalesce-archiving-co')
        account = make_account(tenant)
        active = make_listing(
            tenant, make_product(tenant), account,
            status=Listing.STATUS_ACTIVE, external_id='AV-ACTIVE-1',
        )
        make_listing(
            tenant, make_product_with_article(tenant, 'ART-2'), account,
            status=Listing.STATUS_ARCHIVING, external_id='AV-TARGET-2',
        )

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'), \
             patch('apps.marketplaces.tasks.poll_feed_results_task'):
            mock_cls.return_value.is_autoload_active.return_value = True
            coalesced_flush_task(account.pk)

        flushed = mock_cls.return_value.flush_feed.call_args[0][0]
        assert {i.pk for i in flushed} == {active.pk}

    def test_sends_stop_when_no_active_remains(self):
        """Сняли последнее активное → команда STOP, а не пустой фид."""
        from apps.marketplaces.tasks import coalesced_flush_task
        tenant = make_tenant('coalesce-stop-co')
        account = make_account(tenant)
        make_listing(
            tenant, make_product(tenant), account,
            status=Listing.STATUS_ARCHIVING, external_id='AV-ONLY-1',
        )

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'), \
             patch('apps.marketplaces.tasks.poll_feed_results_task'):
            coalesced_flush_task(account.pk)

        mock_cls.return_value.flush_stop.assert_called_once()
        mock_cls.return_value.flush_feed.assert_not_called()

    def test_retries_on_rate_limit(self):
        """Лимит 1/час → задача уходит в retry, не падает."""
        from celery.exceptions import Retry
        from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError
        from apps.marketplaces.tasks import coalesced_flush_task
        tenant = make_tenant('coalesce-rl-co')
        account = make_account(tenant)
        make_listing(tenant, make_product(tenant), account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'):
            mock_cls.return_value.is_autoload_active.return_value = True
            mock_cls.return_value.flush_feed.side_effect = RateLimitError('1/час')
            with pytest.raises((Retry, RateLimitError)):
                coalesced_flush_task(account.pk)


@pytest.mark.django_db
class TestFeedWindowCoordinator:
    def test_flushes_immediately_when_window_open(self):
        """Окно открыто (давно/никогда не слали) → flush сразу."""
        from apps.marketplaces.tasks import request_feed_flush
        tenant = make_tenant('window-open-co')
        account = make_account(tenant)  # last_feed_flush_at = None → окно открыто

        with patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush:
            request_feed_flush(account)

        assert_exact_feed_flush_scheduled(mock_flush, account)

    def test_defers_one_flush_when_window_closed(self):
        """Окно закрыто (только что слали) → один отложенный flush на момент открытия."""
        from apps.marketplaces.tasks import request_feed_flush
        from django.utils.timezone import now
        tenant = make_tenant('window-closed-co')
        account = make_account(tenant)
        account.last_feed_flush_at = now()
        account.save(update_fields=['last_feed_flush_at'])

        with patch('apps.marketplaces.tasks.coalesced_flush_task') as mock_flush, \
             patch('apps.marketplaces.tasks.cache') as mock_cache:
            mock_cache.add.return_value = True
            request_feed_flush(account)

        mock_flush.delay.assert_not_called()
        mock_flush.apply_async.assert_called_once()
        _, kwargs = mock_flush.apply_async.call_args
        assert kwargs['countdown'] > 0


# ------------------------------------------------------------------ #
#  Bulk listing actions                                               #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
class TestListingBulkActions:
    def test_filtered_bulk_operations_reject_more_rows_than_hard_cap(self, settings):
        settings.API_BULK_MAX_ITEMS = 1
        tenant = make_tenant('bulk-hard-cap-co')
        account = make_account(tenant)
        first = make_listing(tenant, make_product(tenant), account)
        second = make_listing(
            tenant,
            make_product_with_article(tenant, 'ART-CAP-2'),
            account,
        )

        with pytest.raises(ListingBulkLimitExceeded):
            ListingService.bulk_action(tenant, {
                'action': 'update_placement',
                'account_id': account.pk,
                'address_override': 'Москва',
            })
        with pytest.raises(ListingBulkLimitExceeded):
            ListingService.bulk_update_placement(
                tenant,
                {'account_id': account.pk},
                {'address_override': 'Москва'},
            )

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.address_override == ''
        assert second.address_override == ''

    def test_bulk_update_placement_updates_db_and_logs(self):
        tenant = make_tenant('bulk-placement-action-co')
        account = make_account(tenant)
        product = make_product(tenant)
        first = make_listing(tenant, product, account)
        second_product = make_product_with_article(tenant, 'ART-002')
        second = make_listing(tenant, second_product, account)

        result = ListingService.bulk_action(tenant, {
            'action': 'update_placement',
            'listing_ids': [first.pk, second.pk],
            'address_override': 'Москва, Тверская, 1',
            'seller_address_id_override': 'seller-1',
            'manager_name_override': 'Иван',
            'contact_phone_override': '+7 900 100-20-30',
        })

        first.refresh_from_db()
        second.refresh_from_db()
        assert result['success'] == 2
        assert first.address_override == 'Москва, Тверская, 1'
        assert second.seller_address_id_override == 'seller-1'
        assert SyncLog.objects.filter(
            tenant=tenant,
            event_type=SyncLog.EVENT_LISTING_UPDATE,
            listing__in=[first, second],
        ).count() == 2

    def test_update_placement_rejects_account_id_as_address(self):
        """В поле «ID адреса Avito» нельзя сохранить external_id аккаунта."""
        from apps.marketplaces.services import InvalidListingStatus
        tenant = make_tenant('addr-guard-co')
        account = make_account(tenant)
        listing = make_listing(tenant, make_product(tenant), account)

        with pytest.raises(InvalidListingStatus):
            ListingService.update_placement(
                listing.pk, tenant,
                {'seller_address_id_override': account.external_id},
            )

        listing.refresh_from_db()
        assert listing.seller_address_id_override == ''

    def test_bulk_publish_sets_queued_and_respects_tenant(self):
        tenant = make_tenant('bulk-publish-action-co')
        other_tenant = make_tenant('bulk-publish-other-co')
        account = make_account(tenant)
        other_account = make_account(other_tenant)
        listing = make_listing(tenant, make_product(tenant), account)
        other_listing = make_listing(other_tenant, make_product(other_tenant), other_account)

        with patch('apps.marketplaces.services.transaction') as mock_tx:
            mock_tx.on_commit.side_effect = lambda fn: None
            result = ListingService.bulk_action(tenant, {
                'action': 'publish',
                'listing_ids': [listing.pk, other_listing.pk],
            })

        listing.refresh_from_db()
        other_listing.refresh_from_db()
        assert result['total'] == 1
        assert result['success'] == 1
        assert listing.status == Listing.STATUS_QUEUED
        assert other_listing.status == Listing.STATUS_DRAFT

    def test_bulk_archive_and_delete_update_statuses(self):
        tenant = make_tenant('bulk-status-action-co')
        account = make_account(tenant)
        active = make_listing(
            tenant,
            make_product(tenant),
            account,
            status=Listing.STATUS_ACTIVE,
            external_id='active-1',
        )
        second_product = make_product_with_article(tenant, 'ART-002')
        draft = make_listing(tenant, second_product, account)

        with patch('apps.marketplaces.services.transaction') as mock_tx:
            mock_tx.on_commit.side_effect = lambda fn: None
            archive_result = ListingService.bulk_action(tenant, {
                'action': 'archive',
                'listing_ids': [active.pk],
            })
            delete_result = ListingService.bulk_action(tenant, {
                'action': 'delete',
                'listing_ids': [draft.pk],
            })

        active.refresh_from_db()
        draft.refresh_from_db()
        assert archive_result['success'] == 1
        assert delete_result['success'] == 1
        assert active.status == Listing.STATUS_ARCHIVING
        assert draft.status == Listing.STATUS_DELETED

    def test_bulk_publish_skips_invalid_status(self):
        tenant = make_tenant('bulk-skip-action-co')
        account = make_account(tenant)
        listing = make_listing(tenant, make_product(tenant), account, status=Listing.STATUS_ACTIVE)

        result = ListingService.bulk_action(tenant, {
            'action': 'publish',
            'listing_ids': [listing.pk],
        })

        listing.refresh_from_db()
        assert result['success'] == 0
        assert result['skipped'] == 1
        assert listing.status == Listing.STATUS_ACTIVE


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

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_success') as mock_success:
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': ad_id, 'avito_id': 987654}
            ]
            poll_feed_results_task(account.pk)

        listing.refresh_from_db()
        assert listing.external_id == '987654'
        assert listing.status == Listing.STATUS_ACTIVE
        mock_success.assert_called_once()

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
        assert 'не вернул ни ID объявления, ни ошибок' in listing.rejection_reason
        mock_notify.assert_called_once()

    def test_keeps_pending_while_avito_upload_still_processing(self):
        """Ретраи исчерпаны, но загрузка у Avito ещё processing — не отклоняем, ждём дальше."""
        from apps.marketplaces.tasks import poll_feed_results_task

        tenant = make_tenant('poll-still-processing-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': get_ad_id(listing), 'avito_id': None}
            ]
            mock_cls.return_value.get_feed_item_errors.return_value = {}
            mock_cls.return_value.get_latest_upload.return_value = {'status': 'processing'}
            poll_feed_results_task.apply(args=[account.pk], throw=True, retries=10)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        mock_notify.assert_not_called()

    def test_rejection_uses_real_avito_report_messages_immediately(self):
        """Если в отчёте Avito есть ошибки — отклоняем сразу (retries=0) с их текстом, без ожидания ретраев."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import poll_feed_results_task

        tenant = make_tenant('poll-real-errors-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)
        avito_message = '• Неправильно заполнен обязательный параметр — Вид товара.'

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_error'):
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': get_ad_id(listing), 'avito_id': None}
            ]
            mock_cls.return_value.get_feed_item_errors.return_value = {
                get_ad_id(listing): avito_message,
            }
            # Загрузка Avito завершена и свежее нашего flush → ошибки актуальны.
            mock_cls.return_value.get_latest_upload.return_value = {
                'status': 'success_warning',
                'started_at': (now() + timedelta(minutes=1)).isoformat(),
            }
            # retries=0: при наличии ошибок задача НЕ должна уходить в retry
            try:
                poll_feed_results_task.apply(args=[account.pk], throw=True, retries=0)
            except Retry:
                pytest.fail('Должны были отклонить сразу, а не уходить в retry')

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REJECTED
        assert listing.rejection_reason == avito_message

    def test_stale_report_errors_do_not_reject_republished_listing(self):
        """Регрессия: перепубликация с тем же ad_id, свежий фид ещё обрабатывается —
        ошибки из ПРЕДЫДУЩЕЙ загрузки не должны отклонять листинг устаревшей причиной."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import poll_feed_results_task

        tenant = make_tenant('poll-stale-report-co')
        account = make_account(tenant)
        account.last_feed_flush_at = now()
        account.save(update_fields=['last_feed_flush_at'])
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': get_ad_id(listing), 'avito_id': None}
            ]
            # Старый отчёт содержит ошибку по тому же ad_id...
            mock_cls.return_value.get_feed_item_errors.return_value = {
                get_ad_id(listing): '• Старая ошибка из предыдущей загрузки.',
            }
            # ...но свежая загрузка ещё обрабатывается.
            mock_cls.return_value.get_latest_upload.return_value = {'status': 'processing'}
            with pytest.raises((Retry, RuntimeError)):
                poll_feed_results_task(account.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        assert listing.rejection_reason == ''
        mock_notify.assert_not_called()

    def test_old_finished_upload_errors_do_not_reject_new_attempt(self):
        """Ошибки завершённой загрузки, начавшейся ДО нашего flush, не применяются."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import poll_feed_results_task

        tenant = make_tenant('poll-old-upload-co')
        account = make_account(tenant)
        account.last_feed_flush_at = now()
        account.save(update_fields=['last_feed_flush_at'])
        product = make_product(tenant)
        listing = make_listing(tenant, product, account, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_error') as mock_notify:
            mock_cls.return_value.get_feed_results.return_value = [
                {'ad_id': get_ad_id(listing), 'avito_id': None}
            ]
            mock_cls.return_value.get_feed_item_errors.return_value = {
                get_ad_id(listing): '• Старая ошибка.',
            }
            mock_cls.return_value.get_latest_upload.return_value = {
                'status': 'success_warning',
                'started_at': (now() - timedelta(hours=2)).isoformat(),
            }
            with pytest.raises((Retry, RuntimeError)):
                poll_feed_results_task(account.pk)

        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_PENDING
        mock_notify.assert_not_called()

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
        from apps.marketplaces.tasks import coalesced_flush_task, poll_feed_results_task
        tenant = make_tenant('e2e-co')
        account = make_account(tenant)
        ds = DataSourceConnection.objects.create(
            tenant=tenant, name='S', type='1c_http',
            credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
        )

        listings = []
        for i in range(10):
            product, _, _ = ProductService.upsert_from_source(tenant, ds, {
                'uuid': None, 'article': f'E2E{i:03d}', 'name': f'Товар {i}',
                'brand': 'B', 'price': '100', 'stock_qty': 1,
                'category': 'Кузов', 'condition': 'new',
            })
            lst = make_listing(tenant, product, account)
            lst.status = Listing.STATUS_QUEUED
            lst.save(update_fields=['status'])
            listings.append(lst)

        # Шаг 1: единый фид по часовому окну → все QUEUED становятся PENDING
        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks.cache'), \
             patch('apps.marketplaces.tasks.poll_feed_results_task') as mock_poll:
            mock_cls.return_value.is_autoload_active.return_value = True
            mock_cls.return_value.flush_feed.return_value = True
            mock_poll.apply_async = MagicMock()

            coalesced_flush_task(account.pk)

        pending = Listing.objects.filter(tenant=tenant, status=Listing.STATUS_PENDING).count()
        assert pending == 10

        # Шаг 2: poll возвращает avito_ids → статус ACTIVE
        fake_results = [
            {'ad_id': get_ad_id(lst), 'avito_id': 1000 + i}
            for i, lst in enumerate(listings)
        ]
        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_cls, \
             patch('apps.marketplaces.tasks._notify_success'):
            mock_cls.return_value.get_feed_results.return_value = fake_results
            poll_feed_results_task(account.pk)

        active = Listing.objects.filter(tenant=tenant, status=Listing.STATUS_ACTIVE).count()
        assert active == 10
        for i, lst in enumerate(listings):
            lst.refresh_from_db()
            assert lst.external_id == str(1000 + i)
