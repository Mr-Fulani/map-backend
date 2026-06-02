"""
Тесты для StatsService и AnalyticsView (Этап 17).

Покрывает: сохранение ListingStats через StatsService, агрегацию в AnalyticsView,
изоляцию тенантов, идемпотентность upsert-а.
"""
import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, ListingStats, MarketplaceAccount
from apps.marketplaces.services import StatsService
from apps.products.models import Product
from apps.tenants.services import TenantService


# ── Фикстуры ────────────────────────────────────────────────────────────────────

def make_tenant(slug):
    tenant, api_key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant, api_key


def make_account(tenant, external_id='111222333'):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Avito',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=external_id,
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csecret'}),
    )


def make_product(tenant):
    ds = DataSourceConnection.objects.create(
        tenant=tenant, name='DS', type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    return Product.objects.create(
        tenant=tenant, datasource=ds, article='ART-001',
        name='Тестовый товар', price=Decimal('500'), stock_qty=1,
    )


def make_listing(tenant, account, external_id='9990001'):
    product = make_product(tenant)
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        external_id=external_id, status=Listing.STATUS_ACTIVE,
        title='Тестовое объявление',
        description_ai='Описание',
        price_on_listing=Decimal('500'),
    )


def avito_stats_response(item_id: str, days: list[dict]) -> list[dict]:
    """Формирует mock-ответ Avito Stats API."""
    return [{'itemId': int(item_id), 'stats': days}]


# ── StatsService: базовое создание ──────────────────────────────────────────────

@pytest.mark.django_db
def test_fetch_for_account_creates_listing_stats():
    """StatsService создаёт записи ListingStats по данным от Avito."""
    tenant, _ = make_tenant('stats-basic')
    account = make_account(tenant)
    listing = make_listing(tenant, account, external_id='9990001')

    raw = avito_stats_response('9990001', [
        {'date': '2026-05-01', 'views': 100, 'uniqViews': 80, 'contacts': 5},
        {'date': '2026-05-02', 'views': 120, 'uniqViews': 90, 'contacts': 7},
    ])

    with patch.object(StatsService, '_fetch_raw', return_value=raw):
        count = StatsService.fetch_for_account(
            account,
            datetime.date(2026, 5, 1),
            datetime.date(2026, 5, 2),
        )

    assert count == 2
    stats = ListingStats.objects.filter(listing=listing).order_by('date')
    assert stats.count() == 2

    s1 = stats[0]
    assert s1.date == datetime.date(2026, 5, 1)
    assert s1.views == 100
    assert s1.impressions == 80
    assert s1.contacts == 5
    assert s1.tenant == tenant


@pytest.mark.django_db
def test_fetch_for_account_calculates_ctr():
    """CTR считается как views / impressions * 100."""
    tenant, _ = make_tenant('stats-ctr')
    account = make_account(tenant)
    make_listing(tenant, account, external_id='9990002')

    raw = avito_stats_response('9990002', [
        {'date': '2026-05-01', 'views': 10, 'uniqViews': 200, 'contacts': 1},
    ])

    with patch.object(StatsService, '_fetch_raw', return_value=raw):
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    stat = ListingStats.objects.get(date=datetime.date(2026, 5, 1))
    assert stat.ctr == round(10 / 200 * 100, 2)


@pytest.mark.django_db
def test_fetch_for_account_upserts_on_duplicate_date():
    """Повторный вызов за ту же дату обновляет запись, не создаёт дубль."""
    tenant, _ = make_tenant('stats-upsert')
    account = make_account(tenant)
    make_listing(tenant, account, external_id='9990003')

    day = [{'date': '2026-05-01', 'views': 50, 'uniqViews': 100, 'contacts': 2}]
    with patch.object(StatsService, '_fetch_raw', return_value=avito_stats_response('9990003', day)):
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    day_updated = [{'date': '2026-05-01', 'views': 75, 'uniqViews': 120, 'contacts': 3}]
    with patch.object(StatsService, '_fetch_raw', return_value=avito_stats_response('9990003', day_updated)):
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    assert ListingStats.objects.count() == 1
    stat = ListingStats.objects.get()
    assert stat.views == 75
    assert stat.contacts == 3


@pytest.mark.django_db
def test_fetch_for_account_ignores_unknown_item_ids():
    """itemId которого нет в БД — молча пропускается."""
    tenant, _ = make_tenant('stats-unknown-id')
    account = make_account(tenant)

    raw = avito_stats_response('9999999', [
        {'date': '2026-05-01', 'views': 10, 'uniqViews': 20, 'contacts': 1},
    ])
    with patch.object(StatsService, '_fetch_raw', return_value=raw):
        count = StatsService.fetch_for_account(
            account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1),
        )

    assert count == 0
    assert ListingStats.objects.count() == 0


@pytest.mark.django_db
def test_fetch_for_account_skips_non_active_listings():
    """Листинги не в статусе active не включаются в запрос к API."""
    tenant, _ = make_tenant('stats-inactive')
    account = make_account(tenant)
    product = make_product(tenant)
    Listing.objects.create(
        tenant=tenant, product=product, account=account,
        external_id='9990004', status=Listing.STATUS_ARCHIVED,
        title='Архивное', description_ai='...', price_on_listing=Decimal('100'),
    )

    with patch.object(StatsService, '_fetch_raw', return_value=[]) as mock_fetch:
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    mock_fetch.assert_not_called()


@pytest.mark.django_db
def test_fetch_for_account_no_listings_returns_zero():
    """Аккаунт без активных листингов возвращает 0 без обращения к API."""
    tenant, _ = make_tenant('stats-empty')
    account = make_account(tenant)

    with patch.object(StatsService, '_fetch_raw') as mock_fetch:
        count = StatsService.fetch_for_account(
            account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1),
        )

    assert count == 0
    mock_fetch.assert_not_called()


# ── AnalyticsView: агрегация и изоляция ─────────────────────────────────────────

@pytest.mark.django_db
def test_analytics_view_returns_correct_summary(client):
    """GET /api/v1/analytics/ агрегирует ListingStats по тенанту."""
    tenant, api_key = make_tenant('analytics-summary')
    account = make_account(tenant)
    listing = make_listing(tenant, account)

    ListingStats.objects.create(
        listing=listing, tenant=tenant,
        date=datetime.date(2026, 5, 1), views=100, impressions=500, contacts=10, ctr=20.0,
    )
    ListingStats.objects.create(
        listing=listing, tenant=tenant,
        date=datetime.date(2026, 5, 2), views=200, impressions=800, contacts=15, ctr=25.0,
    )

    resp = client.get(
        '/api/v1/analytics/',
        {'date_from': '2026-05-01', 'date_to': '2026-05-02'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert resp.status_code == 200
    summary = resp.json()['data']['summary']
    assert summary['views'] == 300
    assert summary['contacts'] == 25
    assert summary['impressions'] == 1300
    assert summary['avg_ctr'] == round(300 / 1300 * 100, 2)


@pytest.mark.django_db
def test_analytics_view_tenant_isolation(client):
    """Тенант видит только свою статистику."""
    tenant_a, key_a = make_tenant('analytics-iso-a')
    tenant_b, key_b = make_tenant('analytics-iso-b')

    account_a = make_account(tenant_a, external_id='111')
    account_b = make_account(tenant_b, external_id='222')
    listing_a = make_listing(tenant_a, account_a, external_id='8880001')
    listing_b = make_listing(tenant_b, account_b, external_id='8880002')

    ListingStats.objects.create(
        listing=listing_a, tenant=tenant_a,
        date=datetime.date(2026, 5, 1), views=500, impressions=1000, contacts=20, ctr=50.0,
    )
    ListingStats.objects.create(
        listing=listing_b, tenant=tenant_b,
        date=datetime.date(2026, 5, 1), views=9999, impressions=9999, contacts=9999, ctr=100.0,
    )

    resp = client.get(
        '/api/v1/analytics/',
        {'date_from': '2026-05-01', 'date_to': '2026-05-01'},
        HTTP_AUTHORIZATION=f'Bearer {key_a}',
    )

    assert resp.status_code == 200
    assert resp.json()['data']['summary']['views'] == 500


@pytest.mark.django_db
def test_analytics_view_empty_period_returns_zeros(client):
    """За период без данных summary содержит нули."""
    tenant, api_key = make_tenant('analytics-empty')

    resp = client.get(
        '/api/v1/analytics/',
        {'date_from': '2020-01-01', 'date_to': '2020-01-31'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert resp.status_code == 200
    summary = resp.json()['data']['summary']
    assert summary['views'] == 0
    assert summary['contacts'] == 0
    assert summary['avg_ctr'] == 0.0


@pytest.mark.django_db
def test_analytics_view_daily_breakdown(client):
    """daily[] содержит разбивку по дням в хронологическом порядке."""
    tenant, api_key = make_tenant('analytics-daily')
    account = make_account(tenant)
    listing = make_listing(tenant, account)

    for day_num, views in [(1, 10), (2, 20), (3, 30)]:
        ListingStats.objects.create(
            listing=listing, tenant=tenant,
            date=datetime.date(2026, 5, day_num),
            views=views, impressions=100, contacts=1, ctr=10.0,
        )

    resp = client.get(
        '/api/v1/analytics/',
        {'date_from': '2026-05-01', 'date_to': '2026-05-03'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    daily = resp.json()['data']['daily']
    assert len(daily) == 3
    assert daily[0]['date'] == '2026-05-01'
    assert daily[0]['views'] == 10
    assert daily[2]['views'] == 30
