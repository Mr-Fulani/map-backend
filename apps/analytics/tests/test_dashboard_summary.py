from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.analytics.serializers import DashboardSummaryResponseSerializer
from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import AICreditTransaction, AIWallet
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.models import (
    AvitoAccountStatus,
    Listing,
    ListingStats,
    MarketplaceAccount,
)
from apps.products.models import (
    Product,
    ProductCatalogClassification,
    ReviewStatus,
)
from apps.sync.models import SyncLog
from apps.tenants.models import TenantUser
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import membership_access_token, owner_access_token
from apps.web_research.models import WebResearchRun


URL = '/api/v1/dashboard/summary/'


def make_tenant(slug: str):
    tenant, api_key = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@example.test',
        'pass12345',
    )
    return tenant, api_key, owner_access_token(tenant)


def make_datasource(tenant, name='1C', *, status=DataSourceConnection.STATUS_OK):
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name=name,
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({
            'url': 'https://example.test/data',
            'user': 'user',
            'password': 'secret',
        }),
        last_sync_at=timezone.now(),
        last_sync_status=status,
        last_error='Ошибка импорта' if status == DataSourceConnection.STATUS_ERROR else '',
    )


def make_product(tenant, datasource, article):
    return Product.objects.create(
        tenant=tenant,
        datasource=datasource,
        article=article,
        name=f'Товар {article}',
        price=Decimal('1000'),
        stock_qty=2,
    )


def make_account(tenant, suffix='1'):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name=f'Avito {suffix}',
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        external_id=f'avito-{tenant.pk}-{suffix}',
        credentials_enc=encrypt({'client_id': 'client', 'client_secret': 'secret'}),
    )


def make_listing(tenant, product, account, status):
    return Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        status=status,
        title=product.name,
        price_on_listing=product.price,
    )


@pytest.mark.django_db
def test_dashboard_summary_contract_aggregates_actionable_tenant_data():
    tenant, _, token = make_tenant('dashboard-contract')
    datasource = make_datasource(
        tenant,
        status=DataSourceConnection.STATUS_ERROR,
    )
    first_product = make_product(tenant, datasource, 'DASH-1')
    second_product = make_product(tenant, datasource, 'DASH-2')
    account = make_account(tenant)
    active_listing = make_listing(
        tenant,
        first_product,
        account,
        Listing.STATUS_ACTIVE,
    )
    make_listing(
        tenant,
        second_product,
        account,
        Listing.STATUS_REJECTED,
    )
    ListingStats.objects.create(
        tenant=tenant,
        listing=active_listing,
        date=timezone.localdate(),
        views=25,
        contacts=3,
        impressions=100,
        ctr=25,
    )
    ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=first_product,
        domain=ProductCatalogClassification.Domain.AUTO_PARTS,
        source=ProductCatalogClassification.Source.AI,
        confidence=0.6,
        needs_review=True,
        review_status=ReviewStatus.PENDING,
    )
    WebResearchRun.objects.create(
        tenant=tenant,
        product=first_product,
        status=WebResearchRun.Status.NEED_REVIEW,
    )
    SyncLog.objects.create(
        tenant=tenant,
        product=first_product,
        event_type=SyncLog.EVENT_DATASOURCE_IMPORT,
        status=SyncLog.STATUS_ERROR,
        message='Не удалось импортировать строку',
    )
    AvitoAccountStatus.objects.create(
        tenant=tenant,
        account=account,
        connection_status=AvitoAccountStatus.CONNECTION_AUTH_ERROR,
        autoload_status=AvitoAccountStatus.AUTOLOAD_DISABLED,
        tariff_status=AvitoAccountStatus.TARIFF_INACTIVE,
        placement_packages=[{'remain': 2, 'total': 20}],
        last_error_code='unauthorized',
        last_error_message='Ключ отклонён',
    )

    wallet = AIWalletService.ensure_wallet(tenant)
    wallet.purchased_balance = Decimal('15')
    wallet.reserved_balance = Decimal('2')
    wallet.save(update_fields=['purchased_balance', 'reserved_balance'])

    response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    body = response.json()
    contract = DashboardSummaryResponseSerializer(data=body)
    assert contract.is_valid(), contract.errors
    data = body['data']
    assert data['funnel'] == {
        'products': 2,
        'listings': 2,
        'active_listings': 1,
        'queued_listings': 0,
        'pending_listings': 0,
        'rejected_listings': 1,
        'requires_review_listings': 0,
        'limit_reached_listings': 0,
    }
    assert data['analytics']['period_days'] == 30
    assert len(data['analytics']['daily']) == 30
    assert data['analytics']['summary'] == {
        'views': 25,
        'contacts': 3,
        'impressions': 100,
        'avg_ctr': 25.0,
        'active_listings': 1,
    }
    assert data['datasources']['errors'] == 1
    assert data['datasources']['latest_issues'][0]['message'] == 'Ошибка импорта'
    assert data['activity'][0]['code'] == SyncLog.EVENT_DATASOURCE_IMPORT
    assert data['marketplaces']['avito'][0]['connection_status'] == 'auth_error'
    assert data['usage']['ai_credits']['purchased_balance'] == '15.0000'
    assert data['usage']['ai_credits']['reserved_balance'] == '2.0000'
    codes = {item['code'] for item in data['attention']}
    assert {
        'review_queue',
        'research_review',
        'listing_rejected',
        'datasource_errors',
        'avito_account_health',
    }.issubset(codes)
    avito_attention = next(
        item for item in data['attention']
        if item['code'] == 'avito_account_health'
    )
    assert {'profile_stale', 'tariff_stale'}.issubset(
        avito_attention['metadata']['reasons'],
    )
    image_processing = data['services']['image_processing']
    assert image_processing == {
        'available': False,
        'status': 'coming_soon',
        'used': None,
        'limit': None,
        'unit': 'ai_credits',
        'title': 'AI-обработка изображений',
        'description': (
            'Подготовка изображений будет расходовать общий AI-баланс. '
            'Отдельная статистика сервиса пока недоступна.'
        ),
        'uses_shared_ai_balance': True,
        'href': '/dashboard/media',
        'metadata': {
            'billing_model': 'shared_ai_balance',
            'usage_reporting': 'not_available_yet',
        },
    }


@pytest.mark.django_db
def test_dashboard_summary_is_tenant_isolated():
    tenant, _, token = make_tenant('dashboard-isolated')
    own_source = make_datasource(tenant, 'Own')
    make_product(tenant, own_source, 'OWN-1')

    other, _, _ = make_tenant('dashboard-other')
    other_source = make_datasource(
        other,
        'Other',
        status=DataSourceConnection.STATUS_ERROR,
    )
    other_product = make_product(other, other_source, 'OTHER-1')
    other_account = make_account(other)
    make_listing(other, other_product, other_account, Listing.STATUS_REJECTED)
    WebResearchRun.objects.create(
        tenant=other,
        product=other_product,
        status=WebResearchRun.Status.FAILED,
    )
    SyncLog.objects.create(
        tenant=other,
        event_type=SyncLog.EVENT_LISTING_ERROR,
        status=SyncLog.STATUS_ERROR,
        message='Чужая ошибка',
    )

    response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    data = response.json()['data']
    assert data['funnel']['products'] == 1
    assert data['funnel']['listings'] == 0
    assert data['datasources']['total'] == 1
    assert data['datasources']['items'][0]['name'] == 'Own'
    assert data['activity'] == []
    assert 'research_failed' not in {item['code'] for item in data['attention']}


@pytest.mark.django_db
def test_dashboard_listing_counts_match_listing_page_scope():
    tenant, _, token = make_tenant('dashboard-listing-scope')
    datasource = make_datasource(tenant)
    product = make_product(tenant, datasource, 'SCOPE-1')
    inactive_active_product = make_product(tenant, datasource, 'SCOPE-2')
    active_account = make_account(tenant, 'active')
    inactive_account = make_account(tenant, 'inactive')
    inactive_account.is_active = False
    inactive_account.save(update_fields=['is_active'])

    make_listing(
        tenant,
        product,
        active_account,
        Listing.STATUS_REJECTED,
    )
    make_listing(
        tenant,
        product,
        inactive_account,
        Listing.STATUS_REJECTED,
    )
    make_listing(
        tenant,
        inactive_active_product,
        inactive_account,
        Listing.STATUS_ACTIVE,
    )

    client = Client(HTTP_AUTHORIZATION=f'Bearer {token}')
    dashboard_response = client.get(URL)
    listings_response = client.get('/api/v1/listings/?status=rejected')
    billing_response = client.get('/api/v1/billing/usage/')

    assert dashboard_response.status_code == 200
    assert listings_response.status_code == 200
    assert billing_response.status_code == 200
    data = dashboard_response.json()['data']
    rejected_count = data['funnel']['rejected_listings']
    rejected_attention = next(
        item for item in data['attention']
        if item['code'] == 'listing_rejected'
    )
    assert rejected_count == 1
    assert data['funnel']['listings'] == 1
    assert data['funnel']['active_listings'] == 0
    assert data['analytics']['summary']['active_listings'] == 0
    assert data['usage']['listings']['used'] == 1
    assert (
        data['usage']['listings']['used']
        == billing_response.json()['data']['listings']['used']
    )
    assert rejected_attention['count'] == rejected_count
    assert rejected_attention['href'] == '/dashboard/listings?status=rejected'
    assert listings_response.json()['meta']['total'] == rejected_count


@pytest.mark.django_db
def test_dashboard_summary_allows_human_viewer_but_rejects_machine_key():
    tenant, api_key, _ = make_tenant('dashboard-roles')
    membership = TenantService.add_user(
        tenant,
        'dashboard-viewer@example.test',
        role=TenantUser.ROLE_VIEWER,
    )
    viewer_token = membership_access_token(membership)

    viewer_response = Client().get(
        URL,
        HTTP_AUTHORIZATION=f'Bearer {viewer_token}',
    )
    machine_response = Client().get(
        URL,
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    anonymous_response = Client().get(URL)

    assert viewer_response.status_code == 200
    assert machine_response.status_code == 403
    assert anonymous_response.status_code == 401


@pytest.mark.django_db
def test_dashboard_summary_payload_and_queries_stay_bounded():
    tenant, _, token = make_tenant('dashboard-bounded')
    for index in range(25):
        make_datasource(
            tenant,
            f'Source {index:02d}',
            status=DataSourceConnection.STATUS_ERROR,
        )
        make_account(tenant, str(index))
        SyncLog.objects.create(
            tenant=tenant,
            event_type=SyncLog.EVENT_DATASOURCE_IMPORT,
            status=SyncLog.STATUS_ERROR,
            message='Одинаковая ошибка импорта',
        )
    for _index in range(5):
        SyncLog.objects.create(
            tenant=tenant,
            event_type=SyncLog.EVENT_LISTING_UPDATE,
            status=SyncLog.STATUS_OK,
            message='Объявления обновлены',
        )

    with CaptureQueriesContext(connection) as captured:
        response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    data = response.json()['data']
    assert data['datasources']['total'] == 25
    assert data['datasources']['truncated'] is True
    assert len(data['datasources']['items']) == 20
    assert len(data['datasources']['latest_issues']) == 5
    assert data['marketplaces']['avito_total'] == 25
    assert data['marketplaces']['avito_truncated'] is True
    assert len(data['marketplaces']['avito']) == 20
    truncation_attention = next(
        item for item in data['attention']
        if item['code'] == 'avito_accounts_truncated'
    )
    assert truncation_attention['metadata'] == {
        'returned_count': 20,
        'total': 25,
    }
    assert truncation_attention['href'] == '/dashboard/settings#marketplaces'
    assert len(data['activity']) == 2
    grouped_error = next(
        item for item in data['activity'] if item['severity'] == 'error'
    )
    assert grouped_error['metadata']['repeat_count'] == 25
    assert grouped_error['metadata']['aggregation'] == 'event_status_message_excerpt'
    assert grouped_error['metadata']['message_excerpt_length'] == 500
    assert any(item['severity'] == 'success' for item in data['activity'])
    assert len(captured) <= 35


@pytest.mark.django_db
def test_dashboard_summary_does_not_bootstrap_missing_ai_wallet():
    tenant, _, token = make_tenant('dashboard-read-only-wallet')
    AIWallet.objects.filter(tenant=tenant).delete()
    assert not AIWallet.objects.filter(tenant=tenant).exists()
    assert not AICreditTransaction.objects.filter(tenant=tenant).exists()

    with CaptureQueriesContext(connection) as captured:
        response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    credits = response.json()['data']['usage']['ai_credits']
    expected_limit = Decimal(tenant.subscription.plan.limit_ai_credits)
    expected_used = min(Decimal(tenant.ai_credits_used), expected_limit)
    assert Decimal(credits['limit']) == expected_limit
    assert Decimal(credits['used']) == expected_used
    assert Decimal(credits['included_balance']) == expected_limit - expected_used
    assert Decimal(credits['purchased_balance']) == 0
    assert Decimal(credits['reserved_balance']) == 0
    assert not AIWallet.objects.filter(tenant=tenant).exists()
    assert not AICreditTransaction.objects.filter(tenant=tenant).exists()

    dml_queries = [
        query['sql']
        for query in captured.captured_queries
        if query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert dml_queries == []


@pytest.mark.django_db
def test_dashboard_activity_avoids_direct_link_for_mixed_product_ownership():
    tenant, _, token = make_tenant('dashboard-activity-owner')
    datasource = make_datasource(tenant)
    product = make_product(tenant, datasource, 'ACTIVITY-1')
    common = {
        'tenant': tenant,
        'event_type': SyncLog.EVENT_DATASOURCE_IMPORT,
        'status': SyncLog.STATUS_ERROR,
        'message': 'Одинаковая ошибка с разными владельцами',
    }
    SyncLog.objects.create(product=product, **common)
    SyncLog.objects.create(**common)

    response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    activity = next(
        item for item in response.json()['data']['activity']
        if item['message'] == common['message']
    )
    assert activity['metadata']['repeat_count'] == 2
    assert activity['product_id'] is None
    assert activity['listing_id'] is None
    assert activity['href'] == '/dashboard/logs?status=error'


@pytest.mark.django_db
def test_dashboard_ai_attention_distinguishes_purchased_overage_balance():
    tenant, _, token = make_tenant('dashboard-ai-overage')
    wallet = AIWallet.objects.get(tenant=tenant)
    wallet.included_balance = Decimal('0')
    wallet.purchased_balance = Decimal('3')
    wallet.reserved_balance = Decimal('0')
    wallet.save(update_fields=[
        'included_balance', 'purchased_balance', 'reserved_balance',
    ])

    response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    data = response.json()['data']
    credits = data['usage']['ai_credits']
    assert Decimal(credits['available_balance']) == 3
    assert credits['overage_active'] is True
    balance_attention = next(
        item for item in data['attention']
        if item['code'] == 'ai_credit_balance'
    )
    assert balance_attention['severity'] == 'info'
    assert balance_attention['title'] == 'Используются купленные AI-кредиты'
    assert balance_attention['metadata']['overage_active'] is True


@pytest.mark.django_db
def test_dashboard_suppresses_ai_balance_attention_without_subscription():
    tenant, _, token = make_tenant('dashboard-no-subscription')
    wallet = AIWallet.objects.get(tenant=tenant)
    wallet.included_limit = Decimal('0')
    wallet.included_balance = Decimal('0')
    wallet.purchased_balance = Decimal('0')
    wallet.reserved_balance = Decimal('0')
    wallet.save(update_fields=[
        'included_limit', 'included_balance', 'purchased_balance',
        'reserved_balance',
    ])
    tenant.subscription.delete()
    tenant._state.fields_cache.pop('subscription', None)

    response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    codes = {item['code'] for item in response.json()['data']['attention']}
    assert 'subscription_missing' in codes
    assert 'ai_credit_balance' not in codes


@pytest.mark.django_db
def test_dashboard_datasource_attention_ignores_inactive_source_errors():
    tenant, _, token = make_tenant('dashboard-inactive-source')
    datasource = make_datasource(
        tenant,
        status=DataSourceConnection.STATUS_ERROR,
    )
    datasource.is_active = False
    datasource.save(update_fields=['is_active'])

    response = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {token}')

    assert response.status_code == 200
    data = response.json()['data']
    assert data['datasources']['total'] == 1
    assert data['datasources']['active'] == 0
    assert data['datasources']['errors'] == 0
    assert data['datasources']['never_synced'] == 0
    assert data['datasources']['latest_issues'] == []
    assert 'datasource_errors' not in {
        item['code'] for item in data['attention']
    }
