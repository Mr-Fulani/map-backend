"""
Тесты для StatsService и AnalyticsView (Этап 17).

Покрывает: сохранение ListingStats через StatsService, агрегацию в AnalyticsView,
изоляцию тенантов, идемпотентность upsert-а.
"""
import datetime
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import Listing, ListingStats, MarketplaceAccount
from apps.marketplaces.services import StatsService
from apps.products.models import Product
from apps.tenants.tests.auth import create_tenant_with_operator_key


# ── Фикстуры ────────────────────────────────────────────────────────────────────

def make_tenant(slug):
    return create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def make_account(tenant, external_id='111222333'):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Avito',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=external_id,
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csecret'}),
    )


def make_product(tenant, article='ART-001'):
    ds = DataSourceConnection.objects.create(
        tenant=tenant, name='DS', type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    return Product.objects.create(
        tenant=tenant, datasource=ds, article=article,
        name='Тестовый товар', price=Decimal('500'), stock_qty=1,
    )


def make_listing(tenant, account, external_id='9990001', *, article='ART-001'):
    product = make_product(tenant, article=article)
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


@pytest.mark.django_db
def test_avito_adapter_skips_invalid_stats_item_ids_without_losing_valid_ids():
    from apps.marketplaces.adapters.avito.adapter import AvitoAdapter

    tenant, _ = make_tenant('stats-adapter-invalid-id')
    account = make_account(tenant)
    adapter = AvitoAdapter(account)
    response = Mock(status_code=200)
    response.json.return_value = {'result': {'items': []}}

    with (
        patch.object(adapter._auth, 'get_token', return_value='token'),
        patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ) as request,
    ):
        result = adapter.get_stats(
            ['9990010', 'manual-or-corrupt-id', '', '0001'],
            datetime.date(2026, 5, 1),
            datetime.date(2026, 5, 2),
        )

    assert result == []
    assert request.call_args.kwargs['json']['itemIds'] == [9990010]


# ── StatsService: базовое создание ──────────────────────────────────────────────

@pytest.mark.django_db
def test_fetch_for_account_creates_listing_stats():
    """StatsService создаёт записи ListingStats по данным от Avito."""
    tenant, _ = make_tenant('stats-basic')
    account = make_account(tenant)
    listing = make_listing(tenant, account, external_id='9990001')

    raw = avito_stats_response('9990001', [
        {'date': '2026-05-01', 'uniqViews': 80, 'views': 100, 'uniqContacts': 5, 'contacts': 6},
        {'date': '2026-05-02', 'uniqViews': 90, 'views': 120, 'uniqContacts': 7, 'contacts': 9},
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
    assert s1.views == 80        # uniqViews → views
    assert s1.impressions == 100  # views → impressions
    assert s1.contacts == 5      # uniqContacts → contacts
    assert s1.tenant == tenant


@pytest.mark.django_db
def test_fetch_for_account_calculates_ctr():
    """CTR считается как views / impressions * 100."""
    tenant, _ = make_tenant('stats-ctr')
    account = make_account(tenant)
    make_listing(tenant, account, external_id='9990002')

    # uniqViews=20, views=200 → ctr = 20/200*100 = 10.0
    raw = avito_stats_response('9990002', [
        {'date': '2026-05-01', 'uniqViews': 20, 'views': 200, 'uniqContacts': 1, 'contacts': 2},
    ])

    with patch.object(StatsService, '_fetch_raw', return_value=raw):
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    stat = ListingStats.objects.get(date=datetime.date(2026, 5, 1))
    assert stat.ctr == round(20 / 200 * 100, 2)


@pytest.mark.django_db
def test_fetch_for_account_upserts_on_duplicate_date():
    """Повторный вызов за ту же дату обновляет запись, не создаёт дубль."""
    tenant, _ = make_tenant('stats-upsert')
    account = make_account(tenant)
    make_listing(tenant, account, external_id='9990003')

    day = [{'date': '2026-05-01', 'uniqViews': 50, 'views': 100, 'uniqContacts': 2, 'contacts': 3}]
    with patch.object(StatsService, '_fetch_raw', return_value=avito_stats_response('9990003', day)):
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    day_updated = [{'date': '2026-05-01', 'uniqViews': 75, 'views': 120, 'uniqContacts': 4, 'contacts': 5}]
    with patch.object(StatsService, '_fetch_raw', return_value=avito_stats_response('9990003', day_updated)):
        StatsService.fetch_for_account(account, datetime.date(2026, 5, 1), datetime.date(2026, 5, 1))

    assert ListingStats.objects.count() == 1
    stat = ListingStats.objects.get()
    assert stat.views == 75       # uniqViews обновился
    assert stat.contacts == 4     # uniqContacts обновился


@pytest.mark.django_db
def test_fetch_for_account_ignores_unknown_item_ids():
    """itemId которого нет в БД — молча пропускается."""
    tenant, _ = make_tenant('stats-unknown-id')
    account = make_account(tenant)

    raw = avito_stats_response('9999999', [
        {'date': '2026-05-01', 'uniqViews': 10, 'views': 20, 'uniqContacts': 1, 'contacts': 1},
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


@pytest.mark.django_db
def test_invalid_external_id_does_not_block_valid_stats_for_same_account():
    tenant, _ = make_tenant('stats-invalid-external')
    account = make_account(tenant)
    valid = make_listing(
        tenant,
        account,
        external_id='9990010',
        article='STATS-VALID',
    )
    make_listing(
        tenant,
        account,
        external_id='manual-or-corrupt-id',
        article='STATS-INVALID',
    )
    raw = avito_stats_response('9990010', [{
        'date': '2026-05-01',
        'uniqViews': 5,
        'views': 10,
        'uniqContacts': 1,
    }])

    with patch.object(StatsService, '_fetch_raw', return_value=raw) as fetch:
        count = StatsService.fetch_for_account(
            account,
            datetime.date(2026, 5, 1),
            datetime.date(2026, 5, 1),
        )

    assert count == 1
    assert ListingStats.objects.get().listing == valid
    assert fetch.call_args.args[1] == ['9990010']


@pytest.mark.django_db
def test_malformed_provider_days_are_isolated_and_duplicates_are_deduplicated():
    tenant, _ = make_tenant('stats-malformed-response')
    account = make_account(tenant)
    listing = make_listing(tenant, account, external_id='9990011')
    raw = [{
        'itemId': 9990011,
        'stats': [
            {'date': 'not-a-date', 'uniqViews': 100},
            {'date': '2026-05-01', 'uniqViews': -4, 'views': 'bad'},
            {
                'date': '2026-05-01',
                'uniqViews': '7',
                'views': '14',
                'uniqContacts': '9' * 5000,
            },
            {'date': '2026-06-01', 'uniqViews': 500},
        ],
    }, 'not-an-object']

    with patch.object(StatsService, '_fetch_raw', return_value=raw):
        count = StatsService.fetch_for_account(
            account,
            datetime.date(2026, 5, 1),
            datetime.date(2026, 5, 2),
        )

    assert count == 1
    stat = ListingStats.objects.get(listing=listing)
    assert stat.date == datetime.date(2026, 5, 1)
    assert (stat.views, stat.impressions, stat.contacts) == (
        7,
        14,
        StatsService.MAX_COUNTER_VALUE,
    )


def test_stats_range_is_bounded_to_provider_retention():
    with pytest.raises(ValueError, match='270 days'):
        StatsService.fetch_for_account(
            object(),
            datetime.date(2025, 1, 1),
            datetime.date(2025, 10, 1),
        )


@pytest.mark.django_db
@pytest.mark.parametrize(('date_from', 'date_to'), [
    ('not-a-date', '2026-05-01'),
    ('2026-05-02', '2026-05-01'),
    ('2025-01-01', '2025-10-01'),
])
def test_stats_worker_rejects_permanently_invalid_date_ranges(date_from, date_to):
    from apps.marketplaces.tasks import fetch_stats_for_account_task

    tenant, _ = make_tenant(f'stats-invalid-range-{date_from[:4]}-{date_to[-2:]}')
    account = make_account(tenant)

    with patch.object(StatsService, 'fetch_for_account') as fetch:
        result = fetch_stats_for_account_task.run(account.pk, date_from, date_to)

    assert result == {
        'account_id': account.pk,
        'status': 'invalid_date_range',
    }
    fetch.assert_not_called()


@pytest.mark.django_db
def test_hourly_stats_uses_moscow_recovery_window_and_skips_inactive_tenant():
    from apps.marketplaces.tasks import refresh_avito_stats

    active_tenant, _ = make_tenant('stats-scheduler-active')
    inactive_tenant, _ = make_tenant('stats-scheduler-inactive')
    inactive_tenant.is_active = False
    inactive_tenant.save(update_fields=['is_active'])
    active_account = make_account(active_tenant, external_id='stats-active')
    make_account(inactive_tenant, external_id='stats-inactive')
    moscow_today = datetime.date(2026, 8, 28)

    with (
        patch(
            'apps.marketplaces.tasks.timezone.localdate',
            return_value=moscow_today,
        ),
        patch(
            'apps.marketplaces.tasks.fetch_stats_for_account_task.delay',
        ) as fetch,
        patch(
            'apps.anti_ban.tasks.check_shadow_ban_task.delay',
        ) as shadow,
    ):
        result = refresh_avito_stats()

    expected_from = datetime.date(2026, 8, 15)
    assert result == {
        'accounts_scheduled': 1,
        'stats_scheduled': 1,
        'shadow_checks_scheduled': 1,
        'dispatch_failed': 0,
        'date_from': expected_from.isoformat(),
        'date_to': moscow_today.isoformat(),
    }
    fetch.assert_called_once_with(
        active_account.pk,
        expected_from.isoformat(),
        moscow_today.isoformat(),
    )
    shadow.assert_called_once_with(active_account.pk)


@pytest.mark.django_db
def test_shadow_dispatch_failure_does_not_block_stats_dispatch():
    from apps.marketplaces.tasks import refresh_avito_stats

    tenant, _ = make_tenant('stats-scheduler-broker')
    account = make_account(tenant, external_id='stats-broker')

    with (
        patch(
            'apps.anti_ban.tasks.check_shadow_ban_task.delay',
            side_effect=RuntimeError('shadow queue unavailable'),
        ),
        patch(
            'apps.marketplaces.tasks.fetch_stats_for_account_task.delay',
        ) as fetch,
    ):
        result = refresh_avito_stats()

    assert result['stats_scheduled'] == 1
    assert result['shadow_checks_scheduled'] == 0
    assert result['dispatch_failed'] == 1
    fetch.assert_called_once()
    assert fetch.call_args.args[0] == account.pk


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
def test_analytics_view_rejects_reversed_date_range(client):
    _, api_key = make_tenant('analytics-reversed-range')

    resp = client.get(
        '/api/v1/analytics/',
        {'date_from': '2026-05-02', 'date_to': '2026-05-01'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert resp.status_code == 400
    assert resp.json()['code'] == 'invalid_date_range'


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
