from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils.timezone import now

from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.models import TenantUser
from apps.tenants.services import TenantService
from apps.web_research.market import listing_market_comparison
from apps.web_research.models import (
    CompetitorOffer, TenantWebResearchSettings, WebResearchEvidence, WebResearchRun,
)
from apps.web_research.offer_extraction import save_deterministic_offers
from apps.web_research.search_context import build_search_contexts


def make_tenant(slug):
    return TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def make_product(tenant, article='92402D4000'):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        brand='KIA',
        name='Фонарь правый внешний Kia Optima JF',
        category_1c='Автосвет',
        price=Decimal('18000'),
    )


@pytest.mark.django_db
def test_russia_cis_builds_one_context_per_selected_country():
    tenant, _ = make_tenant('geo-cis')
    settings = TenantWebResearchSettings.objects.create(
        tenant=tenant,
        region_preset=TenantWebResearchSettings.RegionPreset.RUSSIA_CIS,
        country_codes=['RU', 'BY', 'KZ'],
        preferred_domains=['example.ru'],
    )

    contexts = build_search_contexts(settings, purpose=WebResearchRun.Purpose.PRICING)

    assert [context.country_code for context in contexts] == ['RU', 'BY', 'KZ']
    assert all(context.market_intent == 'pricing' for context in contexts)
    assert contexts[0].include_domains == ('example.ru',)


@pytest.mark.django_db
def test_json_ld_offer_is_grounded_and_verified_only_for_exact_ru_match():
    tenant, _ = make_tenant('offer-extract')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
        settings_snapshot={
            'region_preset': 'russia', 'country_codes': ['RU'],
            'include_used': False, 'include_preorder': True,
        },
    )
    evidence = WebResearchEvidence.objects.create(
        run=run,
        query='KIA 92402D4000 купить цена',
        rank=1,
        provider_id='tavily',
        title='KIA 92402D4000 — купить в России',
        url='https://parts.example.ru/kia-92402d4000',
        domain='parts.example.ru',
        snippet='Оригинальный фонарь KIA в наличии',
        raw_content='''
        <script type="application/ld+json">
        {"@type":"Product","name":"KIA 92402D4000 фонарь","sku":"92402D4000",
         "brand":{"name":"KIA"},"offers":{"@type":"Offer","price":"14500",
         "priceCurrency":"RUB","availability":"https://schema.org/InStock",
         "seller":{"name":"Parts Example"}}}
        </script>
        ''',
    )

    offers = save_deterministic_offers(run, [evidence], ttl_hours=24)

    assert len(offers) == 1
    offer = offers[0]
    assert offer.evidence == evidence
    assert offer.match_type == CompetitorOffer.MatchType.EXACT
    assert offer.review_status == CompetitorOffer.ReviewStatus.VERIFIED
    assert offer.normalized_price == Decimal('14500.00')
    assert offer.availability == CompetitorOffer.Availability.IN_STOCK


@pytest.mark.django_db
def test_unconfirmed_offer_is_excluded_from_market_statistics():
    tenant, _ = make_tenant('offer-stats')
    product = make_product(tenant)
    account = MarketplaceAccount.objects.create(
        tenant=tenant, name='Avito main', external_id='avito-stats', credentials_enc=b'x',
    )
    listing = Listing.objects.create(
        tenant=tenant, product=product, account=account,
        title=product.name, price_on_listing=Decimal('20000'),
    )
    run = WebResearchRun.objects.create(
        tenant=tenant, product=product, purpose=WebResearchRun.Purpose.PRICING,
    )
    evidence = WebResearchEvidence.objects.create(
        run=run, query='part', rank=1, title='part',
        url='https://shop.example.ru/part', domain='shop.example.ru',
    )
    common = {
        'tenant': tenant, 'product': product, 'run': run, 'evidence': evidence,
        'provider_id': 'brave', 'domain': 'shop.example.ru',
        'url': evidence.url, 'seller_name': 'Shop', 'country_code': 'RU',
        'currency': 'RUB', 'normalized_currency': 'RUB',
        'availability': CompetitorOffer.Availability.IN_STOCK,
        'condition': CompetitorOffer.Condition.NEW,
        'captured_at': now(), 'expires_at': now() + timedelta(hours=24),
    }
    CompetitorOffer.objects.create(
        **common, dedupe_key='a' * 64, price=Decimal('1000'),
        normalized_price=Decimal('1000'), match_type=CompetitorOffer.MatchType.REVIEW,
        review_status=CompetitorOffer.ReviewStatus.PENDING,
    )
    CompetitorOffer.objects.create(
        **{**common, 'url': 'https://shop.example.ru/exact'},
        dedupe_key='b' * 64, price=Decimal('15000'), normalized_price=Decimal('15000'),
        match_type=CompetitorOffer.MatchType.EXACT,
        review_status=CompetitorOffer.ReviewStatus.VERIFIED,
    )

    comparison = listing_market_comparison(listing)

    assert comparison['statistics']['minimum'] == '15000.00'
    assert comparison['statistics']['median'] == '15000.00'
    assert comparison['statistics']['verified_offer_count'] == 1
    assert any('не участвуют' in warning for warning in comparison['warnings'])


@pytest.mark.django_db
def test_settings_are_tenant_scoped_and_operator_cannot_update():
    tenant_a, api_key_a = make_tenant('settings-a')
    tenant_b, _ = make_tenant('settings-b')
    TenantWebResearchSettings.objects.create(
        tenant=tenant_b, region_preset='worldwide', price_ttl_hours=72,
    )
    owner_client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key_a}')

    response = owner_client.put(
        '/api/v1/web-research/settings/',
        data={'region_preset': 'russia', 'price_ttl_hours': 12},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert tenant_a.web_research_settings.price_ttl_hours == 12
    assert tenant_b.web_research_settings.price_ttl_hours == 72

    membership = TenantService.add_user(
        tenant_a, 'operator-settings@test.com', TenantUser.ROLE_OPERATOR,
    )
    membership.user.set_password('pass12345')
    membership.user.save(update_fields=['password'])
    login = Client().post(
        '/api/v1/auth/token/',
        data={
            'email': membership.user.email, 'password': 'pass12345',
            'tenant_slug': tenant_a.slug,
        },
        content_type='application/json',
    )
    operator_client = Client(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")

    assert operator_client.get('/api/v1/web-research/settings/').status_code == 200
    forbidden = operator_client.put(
        '/api/v1/web-research/settings/',
        data={'price_ttl_hours': 48},
        content_type='application/json',
    )
    assert forbidden.status_code == 403


@pytest.mark.django_db
def test_market_research_does_not_start_second_active_pricing_run(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('pricing-active')
    product = make_product(tenant)
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    with patch('apps.web_research.tasks.run_web_research.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/market-research/',
                data={}, content_type='application/json',
            )
        second = client.post(
            f'/api/v1/products/{product.pk}/market-research/',
            data={}, content_type='application/json',
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert WebResearchRun.objects.filter(
        product=product, purpose=WebResearchRun.Purpose.PRICING,
    ).count() == 1
    delay.assert_called_once()


@pytest.mark.django_db
def test_market_offer_endpoint_cannot_read_another_tenant_product():
    tenant_a, api_key = make_tenant('offers-owner')
    tenant_b, _ = make_tenant('offers-other')
    product_b = make_product(tenant_b)

    response = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}').get(
        f'/api/v1/products/{product_b.pk}/market-offers/',
    )

    assert response.status_code == 404
    assert tenant_a.competitor_offers.count() == 0
