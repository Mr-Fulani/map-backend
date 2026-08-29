"""
Тесты для функционала визуального предпросмотра листингов (requires_review).

Покрывает: ListingService.approve, request_regenerate, update_content,
а также API-эндпоинты detail / approve / regenerate / patch.
"""
from concurrent.futures import ThreadPoolExecutor
import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplacePlacementAddress
from apps.marketplaces.services import (
    InvalidListingStatus,
    ListingNotFound,
    ListingPublicationValidationError,
    ListingService,
)
from apps.products.models import Product, ProductImage, TenantCatalogCategory
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import create_operator_key


# ── Фикстуры ───────────────────────────────────────────────────────────────────

def make_tenant(slug):
    """Создаёт тенанта с владельцем."""
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_account(tenant):
    """Создаёт аккаунт маркетплейса."""
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Avito',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id='ext-123',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csecret'}),
        default_address='Москва, Тверская улица, 1',
        default_manager_name='Менеджер',
        default_contact_phone='+79990000000',
    )


def make_product(tenant):
    """Создаёт минимальный продукт."""
    ds = DataSourceConnection.objects.create(
        tenant=tenant,
        name='DS',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    return Product.objects.create(
        tenant=tenant,
        datasource=ds,
        article='ART-001',
        name='Тестовый товар',
        brand='Bosch',
        condition='used',
        price=500,
        stock_qty=1,
    )


def make_listing(tenant, status=Listing.STATUS_REQUIRES_REVIEW):
    """Создаёт листинг с заданным статусом."""
    account = make_account(tenant)
    product = make_product(tenant)
    return Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        status=status,
        title='Тестовый заголовок',
        description_ai='Тестовое AI-описание',
        ai_confidence=0.35,
        price_on_listing=500,
    )


# ── Тесты сервисного слоя ───────────────────────────────────────────────────────

@pytest.mark.django_db
class TestListingServiceApprove:
    def test_approve_changes_status_to_queued(self):
        """Одобрение переводит листинг из requires_review в queued."""
        tenant = make_tenant('approve-co')
        listing = make_listing(tenant)

        with patch('apps.marketplaces.services._enqueue_publish_or_update'):
            result = ListingService.approve(listing.pk, tenant)

        assert result.status == Listing.STATUS_QUEUED

    def test_approve_clears_previous_review_reason(self):
        tenant = make_tenant('approve-clears-reason-co')
        listing = make_listing(tenant)
        listing.rejection_reason = 'Старая ошибка производителя'
        listing.save(update_fields=['rejection_reason'])

        with patch('apps.marketplaces.services._enqueue_publish_or_update'):
            result = ListingService.approve(listing.pk, tenant)

        result.refresh_from_db()
        assert result.status == Listing.STATUS_QUEUED
        assert result.rejection_reason == ''

    def test_approve_enqueues_publish_task(self):
        """Одобрение регистрирует on_commit-хук с задачей публикации."""
        tenant = make_tenant('approve-queue-co')
        listing = make_listing(tenant)

        # on_commit не срабатывает в тестах — подменяем его на немедленный вызов
        with patch('apps.marketplaces.services.transaction') as mock_tx:
            mock_tx.on_commit.side_effect = lambda fn: fn()
            with patch('apps.marketplaces.services._enqueue_publish_or_update') as mock_enqueue:
                ListingService.approve(listing.pk, tenant)

        mock_enqueue.assert_called_once_with(listing.pk, is_new=True)

    def test_approve_blocked_for_non_review_status(self):
        """Нельзя одобрить листинг не в статусе requires_review."""
        tenant = make_tenant('approve-blocked-co')
        listing = make_listing(tenant, status=Listing.STATUS_ACTIVE)

        with pytest.raises(InvalidListingStatus):
            ListingService.approve(listing.pk, tenant)

    def test_approve_raises_for_wrong_tenant(self):
        """Листинг другого тенанта → ListingNotFound."""
        tenant_a = make_tenant('approve-a-co')
        tenant_b = make_tenant('approve-b-co')
        listing = make_listing(tenant_a)

        with pytest.raises(ListingNotFound):
            ListingService.approve(listing.pk, tenant_b)

    def test_approve_blocks_unknown_avito_brand(self):
        tenant = make_tenant('approve-unknown-brand-co')
        listing = make_listing(tenant)
        listing.product.brand = 'НесуществующийБрендXYZ'
        listing.product.condition = 'new'
        listing.product.catalog_category = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Поперечные дуги и комплектующие',
            normalized_name='поперечныедугиикомплектующие',
            domain=TenantCatalogCategory.Domain.AUTO_PARTS,
            external_source='avito',
            external_id='poperechnye_dugi_i_komplektuyushie',
        )
        listing.product.save(update_fields=['brand', 'condition', 'catalog_category'])

        with pytest.raises(ListingPublicationValidationError) as error:
            ListingService.approve(listing.pk, tenant)

        assert 'product_brand' in error.value.field_errors
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_REQUIRES_REVIEW


@pytest.mark.django_db
class TestListingServiceRegenerate:
    def test_regenerate_enqueues_ai_task(self):
        """Перегенерация регистрирует on_commit-хук с задачей генерации AI."""
        tenant = make_tenant('regen-co')
        listing = make_listing(tenant)

        with patch('apps.marketplaces.services.transaction') as mock_tx:
            mock_tx.on_commit.side_effect = lambda fn: fn()
            with patch('apps.marketplaces.services._enqueue_ai_generation') as mock_enqueue:
                ListingService.request_regenerate(listing.pk, tenant)

        mock_enqueue.assert_called_once_with(listing.product_id)

    def test_regenerate_blocked_for_non_review_status(self):
        """Нельзя перегенерировать листинг в статусе active или pending."""
        tenant = make_tenant('regen-blocked-co')
        listing = make_listing(tenant, status=Listing.STATUS_ACTIVE)

        with pytest.raises(InvalidListingStatus):
            ListingService.request_regenerate(listing.pk, tenant)


@pytest.mark.django_db
class TestListingServiceUpdateContent:
    def test_update_title_and_description(self):
        """Обновляет заголовок и AI-описание листинга."""
        tenant = make_tenant('update-co')
        listing = make_listing(tenant)

        result = ListingService.update_content(listing.pk, tenant, 'Новый заголовок', 'Новое описание')

        assert result.title == 'Новый заголовок'
        assert result.description_ai == 'Новое описание'
        assert result.status == Listing.STATUS_REQUIRES_REVIEW  # статус не меняется

    def test_update_only_title(self):
        """Если description_ai не передан — не изменяется."""
        tenant = make_tenant('update-title-co')
        listing = make_listing(tenant)

        result = ListingService.update_content(listing.pk, tenant, 'Новый заголовок', None)

        assert result.title == 'Новый заголовок'
        assert result.description_ai == 'Тестовое AI-описание'  # не тронуто

    def test_update_blocked_for_active_listing(self):
        """Нельзя редактировать активный листинг."""
        tenant = make_tenant('update-active-co')
        listing = make_listing(tenant, status=Listing.STATUS_ACTIVE)

        with pytest.raises(InvalidListingStatus):
            ListingService.update_content(listing.pk, tenant, 'Заголовок', None)

    def test_title_truncated_to_300(self):
        """Заголовок обрезается до 300 символов."""
        tenant = make_tenant('update-truncate-co')
        listing = make_listing(tenant)
        long_title = 'А' * 400

        result = ListingService.update_content(listing.pk, tenant, long_title, None)

        assert len(result.title) == 300


@pytest.mark.django_db
class TestListingDetailAPI:
    def test_regenerate_requires_uuid_and_replays_same_durable_dispatch(
        self,
        django_capture_on_commit_callbacks,
    ):
        from django.test import Client

        from apps.core.models import BackgroundJobDispatch, PaidIngressIntent
        from apps.marketplaces.views import ListingRegenerateView

        tenant = make_tenant('listing-regen-idempotency')
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_DRAFT)
        client = Client(HTTP_AUTHORIZATION=f'Bearer {key}')
        missing = client.post(
            f'/api/v1/listings/{listing.pk}/regenerate/',
            data={},
            content_type='application/json',
        )
        request_key = str(uuid.uuid4())
        submission = {
            'mode': 'generate',
            'job_id': None,
            'dispatch_id': None,
        }

        def submit(_listing_id, _tenant, *, durable_deduplication_key):
            dispatch = BackgroundJobDispatch.objects.create(
                task_name='apps.ai_agent.tasks.generate_description_task',
                queue='ai_generate',
                args=[listing.product_id],
                deduplication_key=durable_deduplication_key,
            )
            submission['dispatch_id'] = str(dispatch.pk)
            listing._regeneration_submission = dict(submission)
            return listing

        with patch.object(
            ListingService, 'request_regenerate', side_effect=submit,
        ) as service:
            first = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={'idempotency_key': request_key},
                content_type='application/json',
            )
            dispatch = BackgroundJobDispatch.objects.get(
                pk=first.json()['data']['dispatch_id'],
            )
            dispatch.status = BackgroundJobDispatch.Status.SUCCEEDED
            dispatch.save(update_fields=['status', 'updated_at'])
            retry = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={'idempotency_key': request_key},
                content_type='application/json',
            )

        assert missing.status_code == 400
        assert first.status_code == 202
        assert retry.status_code == 202
        assert retry.json()['data']['dispatch_id'] == str(dispatch.pk)
        assert retry.json()['data']['state'] == BackgroundJobDispatch.Status.SUCCEEDED
        assert PaidIngressIntent.objects.filter(
            tenant=tenant, operation='listing-regenerate',
        ).count() == 1
        assert service.call_count == 1
        assert ListingRegenerateView.throttle_classes
        assert ListingRegenerateView.expensive_throttle_methods == {'POST'}

    def test_regenerate_same_key_rejects_raw_payload_conflict(self):
        from django.test import Client

        from apps.core.models import BackgroundJobDispatch, PaidIngressIntent

        tenant = make_tenant('listing-regen-payload-conflict')
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_DRAFT)
        client = Client(HTTP_AUTHORIZATION=f'Bearer {key}')
        request_key = str(uuid.uuid4())

        def submit(_listing_id, _tenant, *, durable_deduplication_key):
            dispatch = BackgroundJobDispatch.objects.create(
                task_name='apps.ai_agent.tasks.generate_description_task',
                queue='ai_generate',
                args=[listing.product_id],
                deduplication_key=durable_deduplication_key,
            )
            listing._regeneration_submission = {
                'mode': 'generate',
                'job_id': None,
                'dispatch_id': str(dispatch.pk),
            }
            return listing

        with patch.object(
            ListingService, 'request_regenerate', side_effect=submit,
        ) as service:
            first = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={'idempotency_key': request_key},
                content_type='application/json',
            )
            conflict = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={
                    'idempotency_key': request_key,
                    'unexpected': 'different raw intent',
                },
                content_type='application/json',
            )

        assert first.status_code == 202
        # Strict ingress validation rejects changed raw bytes before a paid
        # action, while the original canonical durable intent remains intact.
        assert conflict.status_code == 400
        assert service.call_count == 1
        assert PaidIngressIntent.objects.filter(
            tenant=tenant,
            operation='listing-regenerate',
        ).count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_regenerate_retries_create_one_dispatch_and_intent(self):
        from django.test import Client

        from apps.core.models import BackgroundJobDispatch, PaidIngressIntent

        tenant = make_tenant('listing-regen-concurrent')
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_DRAFT)
        request_key = str(uuid.uuid4())

        def submit_service(
            _listing_id,
            _tenant,
            *,
            durable_deduplication_key,
        ):
            dispatch = BackgroundJobDispatch.objects.create(
                task_name='apps.ai_agent.tasks.generate_description_task',
                queue='ai_generate',
                args=[listing.product_id],
                deduplication_key=durable_deduplication_key,
            )
            listing._regeneration_submission = {
                'mode': 'generate',
                'job_id': None,
                'dispatch_id': str(dispatch.pk),
            }
            return listing

        def submit():
            close_old_connections()
            try:
                return Client(HTTP_AUTHORIZATION=f'Bearer {key}').post(
                    f'/api/v1/listings/{listing.pk}/regenerate/',
                    data={'idempotency_key': request_key},
                    content_type='application/json',
                ).status_code
            finally:
                close_old_connections()

        with patch.object(
            ListingService,
            'request_regenerate',
            side_effect=submit_service,
        ) as service, patch(
            'apps.core.tasks.execute_background_dispatch.apply_async',
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(lambda _index: submit(), range(2)))

        assert statuses == [202, 202]
        assert service.call_count == 1
        assert PaidIngressIntent.objects.filter(
            tenant=tenant,
            operation='listing-regenerate',
        ).count() == 1
        assert BackgroundJobDispatch.objects.filter(
            task_name='apps.ai_agent.tasks.generate_description_task',
        ).count() == 1

    def test_regenerate_enrichment_charges_parse_budget_once(
        self,
    ):
        from django.test import Client

        from apps.core.models import BackgroundJobDispatch

        tenant = make_tenant('listing-regen-parse-budget')
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_DRAFT)
        client = Client(HTTP_AUTHORIZATION=f'Bearer {key}')
        request_key = str(uuid.uuid4())

        def submit(_listing_id, _tenant, *, durable_deduplication_key):
            dispatch = BackgroundJobDispatch.objects.create(
                task_name=(
                    'apps.products.tasks.'
                    'parse_single_part_then_generate_description'
                ),
                queue='part_parsing',
                args=[123],
                deduplication_key=durable_deduplication_key,
            )
            listing._regeneration_submission = {
                'mode': 'enrich_then_generate',
                'job_id': 123,
                'dispatch_id': str(dispatch.pk),
            }
            return listing

        with patch.object(
            ListingService, 'request_regenerate', side_effect=submit,
        ), patch(
            'apps.marketplaces.views.'
            'consume_transactional_tenant_daily_budget',
        ) as consume:
            first = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={'idempotency_key': request_key},
                content_type='application/json',
            )
            retry = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={'idempotency_key': request_key},
                content_type='application/json',
            )

        assert first.status_code == retry.status_code == 202
        assert retry.json() == first.json()
        consume.assert_called_once_with(
            tenant=tenant,
            scope='product-parse-jobs',
            cost=1,
            limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
        )

    def test_regenerate_parse_budget_exhaustion_rolls_back_dispatch(self):
        from django.test import Client
        from rest_framework.exceptions import Throttled

        from apps.core.models import BackgroundJobDispatch, PaidIngressIntent

        tenant = make_tenant('listing-regen-parse-exhausted')
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_DRAFT)
        client = Client(HTTP_AUTHORIZATION=f'Bearer {key}')

        def submit(_listing_id, _tenant, *, durable_deduplication_key):
            dispatch = BackgroundJobDispatch.objects.create(
                task_name=(
                    'apps.products.tasks.'
                    'parse_single_part_then_generate_description'
                ),
                queue='part_parsing',
                args=[456],
                deduplication_key=durable_deduplication_key,
            )
            listing._regeneration_submission = {
                'mode': 'enrich_then_generate',
                'job_id': 456,
                'dispatch_id': str(dispatch.pk),
            }
            return listing

        with patch.object(
            ListingService, 'request_regenerate', side_effect=submit,
        ), patch(
            'apps.marketplaces.views.'
            'consume_transactional_tenant_daily_budget',
            side_effect=Throttled(wait=60),
        ):
            response = client.post(
                f'/api/v1/listings/{listing.pk}/regenerate/',
                data={'idempotency_key': str(uuid.uuid4())},
                content_type='application/json',
            )

        assert response.status_code == 429
        assert not BackgroundJobDispatch.objects.exists()
        assert not PaidIngressIntent.objects.filter(tenant=tenant).exists()

    def test_patch_changes_account_price_and_placement_address_together(self):
        from django.test import Client

        tenant, key = TenantService.create_tenant(
            'listing-patch-placement-co',
            'listing-patch-placement-co',
            'listing-patch-placement-co@test.com',
            'pass12345',
        )
        key = create_operator_key(tenant)
        old_account = make_account(tenant)
        new_account = MarketplaceAccount.objects.create(
            tenant=tenant,
            name='Second Avito',
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            external_id='ext-456',
            credentials_enc=encrypt({'client_id': 'cid2', 'client_secret': 'csecret2'}),
        )
        product = make_product(tenant)
        listing = Listing.objects.create(
            tenant=tenant,
            product=product,
            account=old_account,
            status=Listing.STATUS_DRAFT,
            title='Тестовый заголовок',
            description_ai='Тестовое AI-описание',
            price_on_listing=500,
        )
        address = MarketplacePlacementAddress.objects.create(
            tenant=tenant,
            account=new_account,
            name='Адрес нового аккаунта',
            address='Москва',
            is_active=True,
        )

        resp = Client().patch(
            f'/api/v1/listings/{listing.pk}/',
            {
                'account_id': new_account.pk,
                'price_on_listing': '777.00',
                'placement_address': address.pk,
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

        assert resp.status_code == 200
        listing.refresh_from_db()
        assert listing.account_id == new_account.pk
        assert listing.price_on_listing == Decimal('777.00')
        assert listing.placement_address_id == address.pk

    def test_archive_and_delete_endpoints_enqueue_tasks(self, django_capture_on_commit_callbacks):
        from django.test import Client

        tenant, key = TenantService.create_tenant(
            'listing-actions-co',
            'listing-actions-co',
            'listing-actions-co@test.com',
            'pass12345',
        )
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_ACTIVE)

        with patch('apps.marketplaces.services._enqueue_unpublish') as unpublish, \
             django_capture_on_commit_callbacks(execute=True):
            archive_resp = Client().post(
                f'/api/v1/listings/{listing.pk}/archive/',
                HTTP_AUTHORIZATION=f'Bearer {key}',
            )
        assert archive_resp.status_code == 200
        unpublish.assert_called_once_with(listing.pk)

        with patch('apps.marketplaces.services._enqueue_delete') as delete, \
             django_capture_on_commit_callbacks(execute=True):
            delete_resp = Client().post(
                f'/api/v1/listings/{listing.pk}/delete/',
                HTTP_AUTHORIZATION=f'Bearer {key}',
            )
        assert delete_resp.status_code == 200
        delete.assert_called_once_with(listing.pk)

    def test_check_status_endpoint_enqueues_feed_poll(self, django_capture_on_commit_callbacks):
        from django.test import Client

        tenant, key = TenantService.create_tenant(
            'listing-check-status-co',
            'listing-check-status-co',
            'listing-check-status-co@test.com',
            'pass12345',
        )
        key = create_operator_key(tenant)
        listing = make_listing(tenant, status=Listing.STATUS_PENDING)

        with patch('apps.marketplaces.services._enqueue_poll_feed_results') as poll, \
             django_capture_on_commit_callbacks(execute=True):
            resp = Client().post(
                f'/api/v1/listings/{listing.pk}/check-status/',
                HTTP_AUTHORIZATION=f'Bearer {key}',
            )

        assert resp.status_code == 200
        poll.assert_called_once_with(listing.account_id)

    def test_bulk_actions_endpoint_publishes_only_tenant_listings(self):
        from django.test import Client

        tenant, key = TenantService.create_tenant(
            'listing-bulk-api-co',
            'listing-bulk-api-co',
            'listing-bulk-api-co@test.com',
            'pass12345',
        )
        key = create_operator_key(tenant)
        other_tenant = make_tenant('listing-bulk-api-other-co')
        listing = make_listing(tenant, status=Listing.STATUS_DRAFT)
        other_listing = make_listing(other_tenant, status=Listing.STATUS_DRAFT)

        with patch('apps.marketplaces.services.transaction') as mock_tx:
            mock_tx.on_commit.side_effect = lambda fn: None
            resp = Client().post(
                '/api/v1/listings/bulk-actions/',
                {
                    'action': 'publish',
                    'listing_ids': [listing.pk, other_listing.pk],
                },
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {key}',
            )

        listing.refresh_from_db()
        other_listing.refresh_from_db()
        assert resp.status_code == 200
        assert resp.json()['data']['success'] == 1
        assert resp.json()['data']['total'] == 1
        assert listing.status == Listing.STATUS_QUEUED
        assert other_listing.status == Listing.STATUS_DRAFT


def test_listing_catalog_category_preserves_null_default_margin():
    """Унаследованная маржа остаётся null, а не строкой ``"None"``."""
    from apps.marketplaces.serializers import ListingDetailSerializer

    category = SimpleNamespace(
        id=7,
        name='Головка блока цилиндров',
        parent_id=None,
        parent=None,
        default_margin_pct=None,
    )
    listing = SimpleNamespace(
        product=SimpleNamespace(catalog_category=category),
    )

    data = ListingDetailSerializer().get_catalog_category(listing)

    assert data['default_margin_pct'] is None
    category.default_margin_pct = Decimal('12.50')
    data = ListingDetailSerializer().get_catalog_category(listing)
    assert data['default_margin_pct'] == '12.50'


@pytest.mark.django_db
class TestListingDetailSerializer:
    def test_detail_includes_product_brand(self):
        """Дровер листинга получает бренд товара для отображения и редактирования."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-brand-co')
        listing = make_listing(tenant)
        listing.product.brand = 'Hyundai-KIA'
        listing.product.save(update_fields=['brand'])

        data = ListingDetailSerializer(listing).data

        assert data['product_brand'] == 'Hyundai-KIA'

    def test_detail_shows_exact_selected_oem_and_multiple_value_warning(self):
        """Drawer exposes both source OEMs and the exact single outgoing value."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-oem-co')
        listing = make_listing(tenant)
        listing.product.oem_numbers = ['92402D5000', '92402D4000']
        listing.product.save(update_fields=['oem_numbers'])

        data = ListingDetailSerializer(listing).data

        assert data['product_oem_numbers'] == ['92402D5000', '92402D4000']
        assert data['product_avito_oem'] == '92402D5000'
        assert 'product_oem' not in data['avito_field_errors']
        assert 'product_oem' in data['avito_field_warnings_by_field']

    def test_detail_includes_last_avito_sync_time(self):
        """Tenant UI can explain when the provider last confirmed the status."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-last-sync-co')
        listing = make_listing(tenant)
        checked_at = timezone.now().replace(microsecond=0)
        listing.last_sync_at = checked_at
        listing.remote_status = Listing.REMOTE_STATUS_ACTIVE
        listing.remote_status_checked_at = checked_at
        listing.next_status_check_at = checked_at + datetime.timedelta(minutes=30)
        listing.save(update_fields=[
            'last_sync_at', 'remote_status', 'remote_status_checked_at',
            'next_status_check_at',
        ])

        data = ListingDetailSerializer(listing).data

        assert parse_datetime(data['last_sync_at']) == checked_at
        assert data['remote_status'] == Listing.REMOTE_STATUS_ACTIVE
        assert parse_datetime(data['remote_status_checked_at']) == checked_at
        assert parse_datetime(data['next_status_check_at']) == (
            checked_at + datetime.timedelta(minutes=30)
        )

    def test_detail_includes_images(self):
        """ListingDetailSerializer возвращает список изображений товара."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-img-co')
        listing = make_listing(tenant)
        ProductImage.objects.create(
            product=listing.product,
            s3_key='products/test.jpg',
            url_source='http://example.com/photo.jpg',
            sha256='abc123',
            position=0,
        )

        listing.refresh_from_db()
        data = ListingDetailSerializer(listing).data

        assert 'images' in data
        assert len(data['images']) == 1
        assert data['images'][0]['position'] == 0

    def test_detail_excludes_images_waiting_for_review_or_rejected(self):
        """Preview листинга показывает только фото, разрешённые к публикации."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-img-filter-co')
        listing = make_listing(tenant)
        ProductImage.objects.create(
            product=listing.product,
            s3_key='products/review.jpg',
            sha256='review',
            position=0,
            status=ProductImage.Status.NEEDS_REVIEW,
        )
        ProductImage.objects.create(
            product=listing.product,
            s3_key='products/rejected.jpg',
            sha256='rejected',
            position=1,
            status=ProductImage.Status.REJECTED,
        )
        approved = ProductImage.objects.create(
            product=listing.product,
            s3_key='products/approved.jpg',
            sha256='approved',
            position=2,
            status=ProductImage.Status.AUTO_APPROVED,
        )

        data = ListingDetailSerializer(listing).data

        assert len(data['images']) == 1
        assert data['images'][0]['id'] == approved.pk

    def test_confidence_display_low(self):
        """ai_confidence_display показывает 'Низкая' при confidence < 0.5."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-conf-low-co')
        listing = make_listing(tenant)
        listing.ai_confidence = 0.34
        listing.save(update_fields=['ai_confidence'])

        data = ListingDetailSerializer(listing).data

        assert 'Низкая' in data['ai_confidence_display']
        assert '34%' in data['ai_confidence_display']

    def test_confidence_display_high(self):
        """ai_confidence_display показывает 'Высокая' при confidence ≥ 0.7."""
        from apps.marketplaces.serializers import ListingDetailSerializer

        tenant = make_tenant('detail-conf-high-co')
        listing = make_listing(tenant)
        listing.ai_confidence = 0.85
        listing.save(update_fields=['ai_confidence'])

        data = ListingDetailSerializer(listing).data

        assert 'Высокая' in data['ai_confidence_display']
