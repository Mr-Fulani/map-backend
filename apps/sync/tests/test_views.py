"""
Тесты для SyncLogListView (GET /api/v1/logs/).

Покрывает: фильтрацию по статусу/типу/дате, пагинацию, изоляцию тенантов.
"""
from decimal import Decimal

import pytest

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.sync.models import SyncLog
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import create_operator_key


# ── Вспомогательные функции ────────────────────────────────────────────────────

def make_tenant(slug):
    """Создаёт тенанта с владельцем и возвращает (tenant, api_key)."""
    tenant, api_key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    api_key = create_operator_key(tenant)
    return tenant, api_key


def make_log(tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH, status=SyncLog.STATUS_OK,
             message='тест', date=None):
    """Создаёт запись SyncLog для тенанта."""
    log = SyncLog.objects.create(
        tenant=tenant,
        event_type=event_type,
        status=status,
        message=message,
    )
    if date:
        SyncLog.objects.filter(pk=log.pk).update(created_at=date)
        log.refresh_from_db()
    return log


# ── Тесты фильтрации по статусу ───────────────────────────────────────────────

@pytest.mark.django_db
def test_filter_status_ok_returns_only_ok_logs(client):
    """Фильтр status=ok возвращает только успешные записи."""
    tenant, api_key = make_tenant('t-status-ok')
    make_log(tenant, status=SyncLog.STATUS_OK)
    make_log(tenant, status=SyncLog.STATUS_ERROR)
    make_log(tenant, status=SyncLog.STATUS_WARN)

    resp = client.get(
        '/api/v1/logs/',
        {'status': 'ok'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert resp.status_code == 200
    data = resp.json()['data']
    assert len(data) == 1
    assert data[0]['status'] == 'ok'


@pytest.mark.django_db
def test_filter_status_warn_returns_only_warn_logs(client):
    """Фильтр status=warn возвращает только предупреждения."""
    tenant, api_key = make_tenant('t-status-warn')
    make_log(tenant, status=SyncLog.STATUS_OK)
    make_log(tenant, status=SyncLog.STATUS_WARN)
    make_log(tenant, status=SyncLog.STATUS_WARN)

    resp = client.get(
        '/api/v1/logs/',
        {'status': 'warn'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert resp.status_code == 200
    data = resp.json()['data']
    assert len(data) == 2
    assert all(d['status'] == 'warn' for d in data)


@pytest.mark.django_db
def test_filter_status_error_returns_only_error_logs(client):
    """Фильтр status=error возвращает только ошибки."""
    tenant, api_key = make_tenant('t-status-error')
    make_log(tenant, status=SyncLog.STATUS_OK)
    make_log(tenant, status=SyncLog.STATUS_ERROR)

    resp = client.get(
        '/api/v1/logs/',
        {'status': 'error'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert resp.status_code == 200
    data = resp.json()['data']
    assert len(data) == 1
    assert data[0]['status'] == 'error'


@pytest.mark.django_db
def test_no_status_filter_returns_all_logs(client):
    """Без фильтра возвращаются все записи тенанта."""
    tenant, api_key = make_tenant('t-no-filter')
    make_log(tenant, status=SyncLog.STATUS_OK)
    make_log(tenant, status=SyncLog.STATUS_WARN)
    make_log(tenant, status=SyncLog.STATUS_ERROR)

    resp = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {api_key}')
    assert resp.status_code == 200
    assert resp.json()['meta']['total'] == 3


# ── Тест фильтрации по типу события ───────────────────────────────────────────

@pytest.mark.django_db
def test_filter_by_event_type(client):
    """Фильтр event_type возвращает только записи нужного типа."""
    tenant, api_key = make_tenant('t-event-type')
    make_log(tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH)
    make_log(tenant, event_type=SyncLog.EVENT_DATASOURCE_IMPORT)
    make_log(tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH)

    resp = client.get(
        '/api/v1/logs/',
        {'event_type': SyncLog.EVENT_LISTING_PUBLISH},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert resp.status_code == 200
    data = resp.json()['data']
    assert len(data) == 2
    assert all(d['event_type'] == SyncLog.EVENT_LISTING_PUBLISH for d in data)


# ── Тест фильтрации по дате ────────────────────────────────────────────────────

@pytest.mark.django_db
def test_filter_by_date(client):
    """Фильтр date возвращает только записи за указанную дату."""
    from datetime import datetime, timezone
    tenant, api_key = make_tenant('t-date-filter')

    past = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    make_log(tenant, date=past)
    make_log(tenant)  # сегодня

    resp = client.get(
        '/api/v1/logs/',
        {'date': '2025-01-15'},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert resp.status_code == 200
    data = resp.json()['data']
    assert len(data) == 1


# ── Тест изоляции тенантов ─────────────────────────────────────────────────────

@pytest.mark.django_db
def test_tenant_isolation_logs_not_visible_to_other_tenant(client):
    """Тенант видит только свои логи, не логи других тенантов."""
    tenant_a, key_a = make_tenant('t-isolation-a')
    tenant_b, key_b = make_tenant('t-isolation-b')

    make_log(tenant_a)
    make_log(tenant_a)
    make_log(tenant_b)

    resp = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {key_a}')
    assert resp.status_code == 200
    assert resp.json()['meta']['total'] == 2

    resp_b = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {key_b}')
    assert resp_b.json()['meta']['total'] == 1


# ── Тест сортировки ────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_logs_ordered_newest_first(client):
    """Логи возвращаются в порядке убывания даты создания."""
    from datetime import datetime, timezone
    tenant, api_key = make_tenant('t-order')

    t1 = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc)
    log1 = make_log(tenant, message='старый', date=t1)
    log2 = make_log(tenant, message='новый', date=t2)

    resp = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {api_key}')
    data = resp.json()['data']
    assert data[0]['id'] == log2.pk
    assert data[1]['id'] == log1.pk


# ── Тест без авторизации ───────────────────────────────────────────────────────

@pytest.mark.django_db
def test_logs_unauthenticated_returns_401(client):
    """Без авторизации запрос возвращает 401."""
    resp = client.get('/api/v1/logs/')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_provider_account_operation_filters_are_tenant_fenced(client):
    tenant, api_key = make_tenant('t-provider-log')
    other, _ = make_tenant('t-provider-log-other')

    def account(owner, suffix):
        return MarketplaceAccount.objects.create(
            tenant=owner,
            name=f'Ozon {suffix}',
            marketplace='ozon',
            external_id=f'ozon-{owner.pk}-{suffix}',
            credentials_enc=encrypt({'test': suffix}),
        )

    def listing(owner, owner_account, suffix):
        product = Product.objects.create(
            tenant=owner,
            article=f'LOG-{suffix}',
            name=f'Product {suffix}',
            price=Decimal('100.00'),
        )
        return Listing.objects.create(
            tenant=owner,
            product=product,
            account=owner_account,
            title=product.name,
            price_on_listing=product.price,
        )

    own_account = account(tenant, 'own')
    own_listing = listing(tenant, own_account, 'own')
    foreign_account = account(other, 'foreign')
    foreign_listing = listing(other, foreign_account, 'foreign')
    own_log = SyncLog.objects.create(
        tenant=tenant,
        listing=own_listing,
        event_type=SyncLog.EVENT_LISTING_PUBLISH,
        status=SyncLog.STATUS_OK,
        message='own',
    )
    make_log(tenant, event_type=SyncLog.EVENT_DATASOURCE_IMPORT)
    SyncLog.objects.create(
        tenant=tenant,
        listing=foreign_listing,
        event_type=SyncLog.EVENT_LISTING_PUBLISH,
        status=SyncLog.STATUS_ERROR,
        message='inconsistent relation must not leak',
    )

    response = client.get(
        '/api/v1/logs/',
        {
            'marketplace': 'ozon',
            'account': own_account.pk,
            'operation': SyncLog.EVENT_LISTING_PUBLISH,
            'provider_result': SyncLog.STATUS_OK,
        },
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    assert response.json()['meta']['total'] == 1
    row = response.json()['data'][0]
    assert row['id'] == own_log.pk
    assert row['marketplace'] == 'ozon'
    assert row['account_id'] == own_account.pk
    assert row['account_name'] == own_account.name
    assert row['operation'] == SyncLog.EVENT_LISTING_PUBLISH
    assert row['provider_result'] == SyncLog.STATUS_OK


@pytest.mark.django_db
def test_log_serializer_hides_inconsistent_cross_tenant_listing_context(client):
    tenant, api_key = make_tenant('t-log-context')
    other, _ = make_tenant('t-log-context-other')
    foreign_account = MarketplaceAccount.objects.create(
        tenant=other,
        name='Foreign',
        marketplace='ozon',
        external_id='foreign-log-context',
        credentials_enc=encrypt({'test': 'foreign'}),
    )
    product = Product.objects.create(
        tenant=other,
        article='FOREIGN-LOG',
        name='Foreign product',
        price=Decimal('100.00'),
    )
    foreign_listing = Listing.objects.create(
        tenant=other,
        product=product,
        account=foreign_account,
        title=product.name,
        price_on_listing=product.price,
    )
    log = SyncLog.objects.create(
        tenant=tenant,
        listing=foreign_listing,
        event_type=SyncLog.EVENT_LISTING_ERROR,
        status=SyncLog.STATUS_ERROR,
        message='broken relation',
    )

    response = client.get(
        '/api/v1/logs/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    row = next(item for item in response.json()['data'] if item['id'] == log.pk)
    assert row['marketplace'] is None
    assert row['account_id'] is None
    assert row['account_name'] is None
