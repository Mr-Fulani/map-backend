from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.channel_listing_index import (
    channel_index_keys,
    hydrate_channel_rows,
)
from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    OzonOfferDraft,
)
from apps.products.models import Product
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


CHANNEL_URL = '/api/v1/listings/channels/'
CHANNEL_ONLY_FIELDS = {
    'resource_id',
    'channel_id',
    'resource_kind',
    'provider_sku',
    'provider_product_id',
}


def _tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant, owner_access_token(tenant)


def _account(tenant, marketplace: str, suffix: str) -> MarketplaceAccount:
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=marketplace,
        name=f'{marketplace.title()} {suffix}',
        external_id=f'{marketplace}-{tenant.pk}-{suffix}',
        credentials_enc=encrypt({'test': suffix}),
    )


def _product(tenant, suffix: str) -> Product:
    return Product.objects.create(
        tenant=tenant,
        article=f'ART-{suffix}',
        name=f'Product {suffix}',
        title_ai=f'AI Product {suffix}',
        brand='Brand',
        price=Decimal('100.00'),
        stock_qty=2,
    )


def _listing(tenant, account, product, *, status=Listing.STATUS_ACTIVE) -> Listing:
    return Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        title=product.title_ai,
        price_on_listing=Decimal('120.00'),
        status=status,
    )


def _ozon_offer(
    tenant,
    account,
    product,
    *,
    publication_status='published',
) -> OzonOfferDraft:
    return OzonOfferDraft.objects.create(
        tenant=tenant,
        account=account,
        product=product,
        publication_status=publication_status,
    )


def _get(client: Client, token: str, params=None):
    return client.get(
        CHANNEL_URL,
        params or {},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


@pytest.mark.django_db
def test_published_ozon_offer_is_an_active_channel_row():
    tenant, token = _tenant('channel-ozon-active')
    account = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    product = _product(tenant, 'ozon-active')
    synced_at = timezone.now().replace(microsecond=0)
    offer = _ozon_offer(tenant, account, product)
    offer.provider_product_id = 123456
    offer.provider_sku = 5692456653
    offer.provider_status = 'processed'
    offer.moderation_status = 'approved'
    offer.last_provider_sync_at = synced_at
    offer.last_synced_price = Decimal('650.84')
    offer.price_override = Decimal('700.00')
    offer.save()

    response = _get(
        Client(),
        token,
        {'marketplace': 'ozon', 'status': 'active'},
    )

    assert response.status_code == 200
    assert response.json()['meta']['total'] == 1
    row = response.json()['data'][0]
    assert row['id'] == offer.pk
    assert row['resource_id'] == offer.pk
    assert row['channel_id'] == f'ozon_offer:{offer.pk}'
    assert row['resource_kind'] == 'ozon_offer'
    assert row['status'] == 'active'
    assert row['status_display'] == 'Активно'
    assert row['marketplace'] == 'ozon'
    assert row['product_id'] == product.pk
    assert row['account_id'] == account.pk
    assert row['price_on_listing'] == '650.84'
    assert row['provider_sku'] == 5692456653
    assert row['provider_product_id'] == 123456
    assert row['external_url'] == 'https://www.ozon.ru/product/5692456653/'
    assert row['last_sync_at'].startswith(synced_at.isoformat()[:19])
    assert row['lifecycle_actions_blocked'] is True
    assert row['can_publish'] is False
    assert row['can_check_provider_status'] is False


@pytest.mark.django_db
def test_avito_channel_presentation_matches_legacy_api_and_shadow_ozon_listing_is_ignored():
    tenant, token = _tenant('channel-avito-compatible')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'primary')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    avito_product = _product(tenant, 'avito-compatible')
    avito_listing = _listing(tenant, avito, avito_product)
    shadow_product = _product(tenant, 'shadow-ozon-listing')
    _listing(tenant, ozon, shadow_product)
    deleted_listing = _listing(
        tenant,
        avito,
        _product(tenant, 'deleted-avito-listing'),
        status=Listing.STATUS_DELETED,
    )

    client = Client()
    legacy_response = client.get(
        '/api/v1/listings/',
        {'marketplace': 'avito'},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    channel_response = _get(client, token, {'marketplace': 'avito'})

    assert legacy_response.status_code == 200
    assert channel_response.status_code == 200
    assert channel_response.json()['meta']['total'] == 1
    channel_row = channel_response.json()['data'][0]
    assert channel_row['id'] == avito_listing.pk
    assert channel_row['resource_kind'] == 'listing'
    assert channel_row['channel_id'] == f'listing:{avito_listing.pk}'
    assert channel_row['id'] != deleted_listing.pk
    legacy_shape = {
        key: value
        for key, value in channel_row.items()
        if key not in CHANNEL_ONLY_FIELDS
    }
    assert legacy_shape == legacy_response.json()['data'][0]


@pytest.mark.django_db
def test_channel_index_enforces_all_tenant_ownership_fences():
    tenant, token = _tenant('channel-fence')
    foreign_tenant, _ = _tenant('channel-fence-foreign')
    own_avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'own')
    own_ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'own')
    foreign_ozon = _account(
        foreign_tenant,
        MarketplaceAccount.MARKETPLACE_OZON,
        'foreign',
    )
    own_product = _product(tenant, 'own')
    foreign_product = _product(foreign_tenant, 'foreign')
    own_listing = _listing(tenant, own_avito, own_product)
    own_offer = _ozon_offer(tenant, own_ozon, own_product)
    _listing(foreign_tenant, _account(
        foreign_tenant,
        MarketplaceAccount.MARKETPLACE_AVITO,
        'foreign',
    ), foreign_product)
    _ozon_offer(foreign_tenant, foreign_ozon, foreign_product)
    # Deliberately inconsistent rows prove every ownership edge is checked.
    _ozon_offer(tenant, own_ozon, foreign_product)
    _ozon_offer(tenant, foreign_ozon, own_product)

    response = _get(Client(), token)

    assert response.status_code == 200
    assert response.json()['meta']['total'] == 2
    assert {
        (row['resource_kind'], row['id'])
        for row in response.json()['data']
    } == {
        ('listing', own_listing.pk),
        ('ozon_offer', own_offer.pk),
    }


@pytest.mark.django_db
def test_channel_filters_global_order_and_pagination_are_applied_in_database():
    tenant, token = _tenant('channel-filters')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'primary')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    avito_listing = _listing(tenant, avito, _product(tenant, 'old-avito'))
    active_offer = _ozon_offer(tenant, ozon, _product(tenant, 'active-ozon'))
    draft_offer = _ozon_offer(
        tenant,
        ozon,
        _product(tenant, 'draft-ozon'),
        publication_status='local_draft',
    )
    queued_offer = _ozon_offer(
        tenant,
        ozon,
        _product(tenant, 'queued-ozon'),
        publication_status='queued',
    )
    now = timezone.now()
    Listing.objects.filter(pk=avito_listing.pk).update(created_at=now - timedelta(minutes=2))
    OzonOfferDraft.objects.filter(pk=active_offer.pk).update(
        created_at=now - timedelta(minutes=1),
    )
    OzonOfferDraft.objects.filter(pk=draft_offer.pk).update(created_at=now)
    OzonOfferDraft.objects.filter(pk=queued_offer.pk).update(
        created_at=now - timedelta(seconds=30),
    )
    client = Client()

    first_page = _get(client, token, {'page_size': 2, 'page': 1})
    second_page = _get(client, token, {'page_size': 2, 'page': 2})
    active = _get(client, token, {'status': 'active'})
    ozon_only = _get(client, token, {'marketplace': 'ozon'})
    avito_account = _get(client, token, {'account': avito.pk})
    capped = _get(client, token, {'page_size': 999})

    assert [row['channel_id'] for row in first_page.json()['data']] == [
        f'ozon_offer:{queued_offer.pk}',
        f'ozon_offer:{active_offer.pk}',
    ]
    assert [row['channel_id'] for row in second_page.json()['data']] == [
        f'listing:{avito_listing.pk}',
    ]
    assert first_page.json()['meta']['total'] == 3
    assert first_page.json()['meta']['page_size'] == 2
    assert {row['channel_id'] for row in active.json()['data']} == {
        f'listing:{avito_listing.pk}',
        f'ozon_offer:{active_offer.pk}',
    }
    assert ozon_only.json()['meta']['total'] == 2
    assert {row['id'] for row in ozon_only.json()['data']} == {
        queued_offer.pk,
        active_offer.pk,
    }
    assert all(row['id'] != draft_offer.pk for row in ozon_only.json()['data'])
    assert [row['id'] for row in avito_account.json()['data']] == [avito_listing.pk]
    assert capped.json()['meta']['page_size'] == 500


@pytest.mark.django_db
def test_every_ozon_publication_state_is_normalized_fail_closed():
    tenant, token = _tenant('channel-status-normalization')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    expected = {
        'queued': Listing.STATUS_QUEUED,
        'import_processing': Listing.STATUS_PENDING,
        'moderation_pending': Listing.STATUS_PENDING,
        'outcome_unknown': Listing.STATUS_PENDING,
        'published': Listing.STATUS_ACTIVE,
        'send_failed': Listing.STATUS_REJECTED,
        'not_accepted': Listing.STATUS_REJECTED,
        'import_failed': Listing.STATUS_REJECTED,
        'moderation_failed': Listing.STATUS_REJECTED,
        'archived': Listing.STATUS_ARCHIVED,
        'manual_review': Listing.STATUS_REQUIRES_REVIEW,
        'future_provider_state': Listing.STATUS_REQUIRES_REVIEW,
    }
    status_by_offer_id = {}
    for index, (source_status, normalized_status) in enumerate(expected.items()):
        offer = _ozon_offer(
            tenant,
            ozon,
            _product(tenant, f'normalize-{index}'),
            publication_status=source_status,
        )
        status_by_offer_id[offer.pk] = normalized_status

    response = _get(Client(), token, {'marketplace': 'ozon', 'page_size': 500})
    review_response = _get(
        Client(),
        token,
        {'marketplace': 'ozon', 'status': Listing.STATUS_REQUIRES_REVIEW},
    )

    assert response.status_code == 200
    assert {
        row['id']: row['status'] for row in response.json()['data']
    } == status_by_offer_id
    assert {
        row['id'] for row in review_response.json()['data']
    } == {
        offer_id
        for offer_id, normalized_status in status_by_offer_id.items()
        if normalized_status == Listing.STATUS_REQUIRES_REVIEW
    }


@pytest.mark.django_db
def test_failed_update_of_previously_confirmed_offer_requires_review():
    tenant, token = _tenant('channel-prior-live-failure')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    initial_failure = _ozon_offer(
        tenant,
        ozon,
        _product(tenant, 'initial-failure'),
        publication_status='send_failed',
    )
    prior_live_failure = _ozon_offer(
        tenant,
        ozon,
        _product(tenant, 'prior-live-failure'),
        publication_status='send_failed',
    )
    OzonOfferDraft.objects.filter(pk=prior_live_failure.pk).update(
        provider_product_id=123456,
        last_provider_sync_at=timezone.now(),
    )

    response = _get(Client(), token, {'marketplace': 'ozon', 'page_size': 500})
    rejected = _get(Client(), token, {
        'marketplace': 'ozon',
        'status': Listing.STATUS_REJECTED,
    })
    review = _get(Client(), token, {
        'marketplace': 'ozon',
        'status': Listing.STATUS_REQUIRES_REVIEW,
    })

    by_id = {row['id']: row for row in response.json()['data']}
    assert by_id[initial_failure.pk]['status'] == Listing.STATUS_REJECTED
    assert by_id[prior_live_failure.pk]['status'] == Listing.STATUS_REQUIRES_REVIEW
    assert [row['id'] for row in rejected.json()['data']] == [initial_failure.pk]
    assert [row['id'] for row in review.json()['data']] == [prior_live_failure.pk]
    assert 'ранее подтверждённая версия' in by_id[prior_live_failure.pk]['status_explanation']


@pytest.mark.django_db
def test_enrichment_local_draft_is_not_a_listing_until_publication_is_attempted():
    tenant, token = _tenant('channel-enrichment-draft')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    technical_draft = _ozon_offer(
        tenant,
        ozon,
        _product(tenant, 'enrichment-only'),
        publication_status='local_draft',
    )
    attempted = {
        status: _ozon_offer(
            tenant,
            ozon,
            _product(tenant, f'attempted-{status}'),
            publication_status=status,
        )
        for status in ('queued', 'import_processing', 'published', 'send_failed')
    }

    response = _get(Client(), token, {'marketplace': 'ozon', 'page_size': 500})

    assert response.status_code == 200
    assert response.json()['meta']['total'] == len(attempted)
    assert {row['id'] for row in response.json()['data']} == {
        offer.pk for offer in attempted.values()
    }
    assert technical_draft.pk not in {
        row['id'] for row in response.json()['data']
    }


@pytest.mark.django_db
def test_equal_numeric_ids_from_different_resources_remain_distinct():
    tenant, token = _tenant('channel-resource-identity')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'primary')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    shared_id = 900001
    listing_product = _product(tenant, 'same-id-avito')
    offer_product = _product(tenant, 'same-id-ozon')
    listing = Listing.objects.create(
        pk=shared_id,
        tenant=tenant,
        product=listing_product,
        account=avito,
        title=listing_product.title_ai,
        price_on_listing=Decimal('120.00'),
        status=Listing.STATUS_ACTIVE,
    )
    offer = OzonOfferDraft.objects.create(
        pk=shared_id,
        tenant=tenant,
        product=offer_product,
        account=ozon,
        publication_status='published',
    )
    same_created_at = timezone.now()
    Listing.objects.filter(pk=listing.pk).update(created_at=same_created_at)
    OzonOfferDraft.objects.filter(pk=offer.pk).update(created_at=same_created_at)

    response = _get(Client(), token)

    assert response.status_code == 200
    assert response.json()['meta']['total'] == 2
    assert [row['channel_id'] for row in response.json()['data']] == [
        f'listing:{shared_id}',
        f'ozon_offer:{shared_id}',
    ]
    assert [row['product_id'] for row in response.json()['data']] == [
        listing_product.pk,
        offer_product.pk,
    ]


@pytest.mark.django_db
def test_channel_page_hydration_query_count_is_constant_without_n_plus_one():
    tenant, _ = _tenant('channel-query-count')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'primary')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    for index in range(15):
        _listing(tenant, avito, _product(tenant, f'avito-query-{index}'))
        _ozon_offer(tenant, ozon, _product(tenant, f'ozon-query-{index}'))

    with CaptureQueriesContext(connection) as captured:
        keys = list(channel_index_keys(tenant)[:500])
        rows = hydrate_channel_rows(tenant, keys)

    assert len(rows) == 30
    assert len(captured.captured_queries) == 3


@pytest.mark.django_db
def test_filtered_hydration_fails_closed_during_concurrent_status_change():
    tenant, _ = _tenant('channel-status-race')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    offer = _ozon_offer(tenant, ozon, _product(tenant, 'status-race'))
    keys = list(channel_index_keys(
        tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        normalized_status=Listing.STATUS_ACTIVE,
    ))
    OzonOfferDraft.objects.filter(pk=offer.pk).update(
        publication_status='moderation_failed',
    )

    rows = hydrate_channel_rows(
        tenant,
        keys,
        expected_status=Listing.STATUS_ACTIVE,
    )

    assert rows == []
