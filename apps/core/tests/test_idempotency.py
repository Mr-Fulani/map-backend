"""
Тесты идемпотентности и изоляции тенантов.

Покрывают требования Этапа 12:
- Celery-задачи безопасны при повторном вызове (нет дублей)
- Данные тенанта A не попадают в queryset тенанта B
- Периодические задачи выполняются без ошибок
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@t.com', 'pass')
    return tenant


def make_datasource(tenant):
    return DataSourceConnection.objects.create(
        tenant=tenant, name='DS', type='1c_http',
        credentials=encrypt({'url': 'http://x', 'user': 'u', 'password': 'p'}),
    )


def make_product(tenant, datasource, article='ART-001'):
    return Product.objects.create(
        tenant=tenant, datasource=datasource,
        article=article, name='Деталь', price=Decimal('100'), stock_qty=1,
    )


def make_account(tenant):
    return MarketplaceAccount.objects.create(
        tenant=tenant, name='Acc', external_id='1',
        credentials_enc=encrypt({'client_id': 'x', 'client_secret': 'y'}),
    )


def make_listing(tenant, product, account, external_id=None):
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        price_on_listing=Decimal('100'), title='Деталь',
        status=Listing.STATUS_ACTIVE if external_id else Listing.STATUS_DRAFT,
        external_id=external_id,
    )


@pytest.mark.django_db
class TestPublishTaskIdempotency:
    def test_already_published_listing_exits_early(self):
        """publish_listing_task на уже опубликованном листинге возвращает None без API-вызова."""
        from apps.marketplaces.tasks import publish_listing_task

        tenant = make_tenant('idem-pub')
        ds = make_datasource(tenant)
        product = make_product(tenant, ds)
        account = make_account(tenant)
        listing = make_listing(tenant, product, account, external_id='avito-123')

        with patch('apps.marketplaces.tasks.AvitoAdapter') as mock_adapter:
            result = publish_listing_task(listing.pk)

        # Нет вызова Avito API — задача завершилась на early return
        mock_adapter.assert_not_called()
        assert result is None

    def test_listing_not_duplicated_on_retry(self):
        """Повторный вызов publish_listing_task не создаёт дублей в БД."""
        from apps.marketplaces.tasks import publish_listing_task

        tenant = make_tenant('idem-retry')
        ds = make_datasource(tenant)
        product = make_product(tenant, ds, 'ART-RETRY')
        account = make_account(tenant)
        listing = make_listing(tenant, product, account, external_id='avito-retry')

        # Вызываем задачу дважды — оба раза должны выйти на early return
        with patch('apps.marketplaces.tasks.AvitoAdapter'):
            publish_listing_task(listing.pk)
            publish_listing_task(listing.pk)

        assert Listing.objects.filter(tenant=tenant, external_id='avito-retry').count() == 1


@pytest.mark.django_db
class TestTenantIsolation:
    def test_products_isolated_between_tenants(self):
        """Queryset товаров тенанта A не содержит товары тенанта B."""
        t1 = make_tenant('iso-prod-1')
        t2 = make_tenant('iso-prod-2')
        ds1 = make_datasource(t1)
        ds2 = make_datasource(t2)

        make_product(t1, ds1, 'T1-001')
        make_product(t2, ds2, 'T2-001')

        t1_articles = list(Product.objects.filter(tenant=t1).values_list('article', flat=True))
        t2_articles = list(Product.objects.filter(tenant=t2).values_list('article', flat=True))

        assert 'T1-001' in t1_articles
        assert 'T2-001' not in t1_articles
        assert 'T2-001' in t2_articles
        assert 'T1-001' not in t2_articles

    def test_listings_isolated_between_tenants(self):
        """Листинги тенанта A не видны тенанту B."""
        t1 = make_tenant('iso-list-1')
        t2 = make_tenant('iso-list-2')
        ds1 = make_datasource(t1)
        ds2 = make_datasource(t2)
        p1 = make_product(t1, ds1, 'ISO-L1')
        p2 = make_product(t2, ds2, 'ISO-L2')
        a1 = make_account(t1)
        a2 = make_account(t2)
        make_listing(t1, p1, a1, external_id='ext-t1')
        make_listing(t2, p2, a2, external_id='ext-t2')

        t1_externals = list(Listing.objects.filter(tenant=t1).values_list('external_id', flat=True))
        assert 'ext-t1' in t1_externals
        assert 'ext-t2' not in t1_externals

    def test_synclog_isolated_between_tenants(self):
        """SyncLog тенанта A не показывается тенанту B через API."""
        from apps.sync.models import SyncLog
        from apps.tenants.models import APIKey

        t1 = make_tenant('iso-log-1')
        t2 = make_tenant('iso-log-2')
        _, key1 = APIKey.generate(t1, 'k')
        SyncLog.objects.create(tenant=t1, event_type='billing_event', status='ok', message='t1')
        SyncLog.objects.create(tenant=t2, event_type='billing_event', status='ok', message='t2')

        from django.test import Client
        c = Client()
        resp = c.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {key1}')
        messages = [d['message'] for d in resp.json()['data']]
        assert 't1' in messages
        assert 't2' not in messages


@pytest.mark.django_db
class TestPeriodicTasksSmoke:
    """Дымовые тесты периодических задач — проверяют, что задачи не падают."""

    def test_sync_all_tenants_runs_without_error(self):
        """sync_all_tenants запускается без исключений (0 соединений = 0 задач)."""
        from apps.sync.tasks import sync_all_tenants

        result = sync_all_tenants()
        assert result['connections_queued'] == 0

    def test_update_tenant_counters_runs_without_error(self):
        """update_tenant_counters запускается и возвращает словарь."""
        from apps.tenants.tasks import update_tenant_counters

        result = update_tenant_counters()
        assert 'tenants_updated' in result

    def test_reconcile_listings_runs_without_error(self):
        """reconcile_listings запускается без исключений."""
        from apps.marketplaces.tasks import reconcile_listings

        result = reconcile_listings()
        assert 'listings_reconciled' in result

    def test_refresh_avito_stats_runs_without_error(self):
        """refresh_avito_stats запускается без исключений."""
        from apps.marketplaces.tasks import refresh_avito_stats

        result = refresh_avito_stats()
        assert 'accounts_scheduled' in result

    def test_cleanup_old_logs_runs_without_error(self):
        """cleanup_old_logs запускается и возвращает счётчик удалённых."""
        from apps.notifications.tasks import cleanup_old_logs

        result = cleanup_old_logs()
        assert 'deleted' in result
