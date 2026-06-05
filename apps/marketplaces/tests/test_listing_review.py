"""
Тесты для функционала визуального предпросмотра листингов (requires_review).

Покрывает: ListingService.approve, request_regenerate, update_content,
а также API-эндпоинты detail / approve / regenerate / patch.
"""
import pytest
from unittest.mock import patch

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.marketplaces.services import InvalidListingStatus, ListingNotFound, ListingService
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService


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
    def test_approve_changes_status_to_draft(self):
        """Одобрение переводит листинг из requires_review в draft."""
        tenant = make_tenant('approve-co')
        listing = make_listing(tenant)

        with patch('apps.marketplaces.services._enqueue_publish_or_update'):
            result = ListingService.approve(listing.pk, tenant)

        assert result.status == Listing.STATUS_DRAFT

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
class TestListingDetailSerializer:
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
