from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils.timezone import now

from apps.anti_ban.ramp_up import GradualRampUp
from apps.anti_ban.velocity import VelocityController
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.services import ProductService
from apps.sync.models import SyncLog
from apps.tenants.services import TenantService


def make_tenant(slug, days_ago: int = 0):
    """Создаёт тенанта с created_at на days_ago дней назад."""
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    if days_ago:
        from apps.tenants.models import Tenant
        Tenant.objects.filter(pk=tenant.pk).update(
            created_at=now() - timedelta(days=days_ago)
        )
        tenant.refresh_from_db()
    return tenant


def make_account(tenant):
    """Создаёт MarketplaceAccount для тенанта."""
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Account',
        external_id='99999',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csec'}),
    )


def make_listing(tenant, account):
    """Создаёт листинг для тенанта."""
    ds = DataSourceConnection.objects.create(
        tenant=tenant, name='S', type='1c_http',
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    product, _, _ = ProductService.upsert_from_source(tenant, ds, {
        'uuid': None, 'article': 'ART-001', 'name': 'Деталь',
        'brand': 'B', 'price': '100', 'stock_qty': 1,
        'category': 'Кузов', 'condition': 'new',
    })
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        price_on_listing=Decimal('100'), title='Деталь',
        status=Listing.STATUS_DRAFT,
    )


@pytest.mark.django_db
class TestGradualRampUp:
    def test_day_1_limit_is_100(self):
        """Новый тенант (день 1) может опубликовать не более 100 объявлений."""
        tenant = make_tenant('day1-co', days_ago=0)
        assert GradualRampUp().get_daily_limit(tenant) == 100

    def test_day_3_limit_is_500(self):
        """На 3-й день лимит поднимается до 500."""
        tenant = make_tenant('day3-co', days_ago=2)
        assert GradualRampUp().get_daily_limit(tenant) == 500

    def test_day_30_is_unlimited(self):
        """После 30 дней ограничений нет."""
        tenant = make_tenant('day30-co', days_ago=30)
        limit = GradualRampUp().get_daily_limit(tenant)
        assert limit > 10_000

    def test_new_tenant_blocked_at_100(self):
        """Новый тенант с 100 публикациями сегодня не может добавить ещё одну."""
        tenant = make_tenant('block-co', days_ago=0)
        assert GradualRampUp().is_allowed(tenant, published_today=99) is True
        assert GradualRampUp().is_allowed(tenant, published_today=100) is False

    def test_get_published_today_counts_only_todays_active(self):
        """Только активные публикации за сегодня попадают в счётчик."""
        tenant = make_tenant('count-co', days_ago=0)
        account = make_account(tenant)
        listing = make_listing(tenant, account)
        listing.status = Listing.STATUS_ACTIVE
        listing.published_at = now()
        listing.save()

        count = GradualRampUp().get_published_today(tenant)
        assert count == 1

    def test_published_yesterday_not_counted(self):
        """Публикации вчерашнего дня не влияют на сегодняшний счётчик."""
        tenant = make_tenant('yesterday-co', days_ago=1)
        account = make_account(tenant)
        listing = make_listing(tenant, account)
        listing.status = Listing.STATUS_ACTIVE
        listing.published_at = now() - timedelta(days=1)
        listing.save()

        count = GradualRampUp().get_published_today(tenant)
        assert count == 0


@pytest.mark.django_db
class TestVelocityController:
    def test_allows_within_limit(self, settings):
        """Запросы в пределах лимита разрешаются."""
        tenant = make_tenant('vel-ok-co')
        account = make_account(tenant)
        controller = VelocityController()
        # publish лимит = 50, первые 10 запросов должны пройти
        for _ in range(10):
            assert controller.is_allowed(account, 'publish') is True

    def test_blocks_when_limit_exceeded(self):
        """После превышения лимита операция блокируется."""
        tenant = make_tenant('vel-block-co')
        account = make_account(tenant)
        controller = VelocityController()

        # Подменяем Redis — устанавливаем счётчик за лимит
        with patch('apps.anti_ban.velocity.cache') as mock_cache:
            mock_cache.incr.return_value = 51  # превышаем лимит 50 для publish
            mock_cache.expire.return_value = True
            mock_cache.get.return_value = 0
            result = controller.is_allowed(account, 'publish')

        assert result is False

    def test_velocity_exceeded_creates_synclog(self):
        """При превышении velocity в SyncLog появляется запись anti_ban_trigger."""
        tenant = make_tenant('vel-log-co')
        account = make_account(tenant)
        controller = VelocityController()

        with patch('apps.anti_ban.velocity.cache') as mock_cache:
            mock_cache.incr.return_value = 51
            mock_cache.expire.return_value = True
            mock_cache.get.return_value = 0
            controller.is_allowed(account, 'publish')

        log = SyncLog.objects.filter(
            tenant=tenant,
            event_type=SyncLog.EVENT_ANTI_BAN,
        ).first()
        assert log is not None
        assert log.status == SyncLog.STATUS_WARN

    def test_tenant_isolation_in_velocity(self):
        """Счётчик velocity одного тенанта не влияет на другого."""
        t1 = make_tenant('iso1-vel')
        t2 = make_tenant('iso2-vel')
        a1 = make_account(t1)
        a2 = make_account(t2)
        controller = VelocityController()

        # a1 превышает лимит
        with patch('apps.anti_ban.velocity.cache') as mock_cache:
            mock_cache.incr.return_value = 51
            mock_cache.expire.return_value = True
            mock_cache.get.return_value = 0
            assert controller.is_allowed(a1, 'publish') is False

        # a2 не должен быть заблокирован — у него свой ключ
        assert controller.get_current_count(a2, 'publish') == 0


@pytest.mark.django_db
class TestPublishTaskWithAntiBan:
    def test_ramp_up_limit_causes_retry(self):
        """publish_listing_task откладывается если достигнут ramp-up лимит."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import publish_listing_task

        tenant = make_tenant('ramp-retry-co', days_ago=0)
        account = make_account(tenant)
        listing = make_listing(tenant, account)

        with patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks.GradualRampUp') as mock_ramp, \
             patch('apps.marketplaces.tasks.VelocityController'):
            mock_cache.lock.return_value.__enter__ = lambda s: None
            mock_cache.lock.return_value.__exit__ = lambda s, *a: False
            mock_cache.get.return_value = None
            mock_ramp.return_value.get_published_today.return_value = 100
            mock_ramp.return_value.is_allowed.return_value = False  # Лимит достигнут

            with pytest.raises((Retry, RuntimeError)):
                publish_listing_task(listing.pk)

    def test_velocity_limit_causes_retry(self):
        """publish_listing_task откладывается при превышении velocity."""
        from celery.exceptions import Retry
        from apps.marketplaces.tasks import publish_listing_task

        tenant = make_tenant('vel-retry-co', days_ago=31)
        account = make_account(tenant)
        listing = make_listing(tenant, account)

        with patch('apps.marketplaces.tasks.cache') as mock_cache, \
             patch('apps.marketplaces.tasks.GradualRampUp') as mock_ramp, \
             patch('apps.marketplaces.tasks.VelocityController') as mock_vel:
            mock_cache.lock.return_value.__enter__ = lambda s: None
            mock_cache.lock.return_value.__exit__ = lambda s, *a: False
            mock_cache.get.return_value = None
            mock_ramp.return_value.get_published_today.return_value = 0
            mock_ramp.return_value.is_allowed.return_value = True
            mock_vel.return_value.is_allowed.return_value = False  # Velocity превышена

            with pytest.raises((Retry, RuntimeError)):
                publish_listing_task(listing.pk)
