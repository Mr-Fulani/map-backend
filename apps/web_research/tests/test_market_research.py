from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import uuid
from unittest.mock import patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.db import close_old_connections
from django.test import Client
from django.utils.timezone import now

from apps.core.models import BackgroundJobDispatch, PaidIngressIntent
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.models import TenantUser
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import (
    create_operator_key, create_tenant_with_operator_key,
    owner_client as make_owner_client,
)
from apps.web_research.market import _difference, listing_market_comparison
from apps.web_research.models import (
    CompetitorOffer, TenantWebResearchSettings, WebResearchEvidence, WebResearchRun,
)
from apps.web_research.offer_extraction import save_deterministic_offers
from apps.web_research.prompts import WEB_RESEARCH_OUTPUT_SCHEMA
from apps.web_research.search_context import build_search_contexts, localize_query


@pytest.fixture(autouse=True)
def local_expensive_start_cache(monkeypatch, request):
    from apps.core import throttling

    cache = LocMemCache(f'market-research-start-{request.node.nodeid}', {})
    monkeypatch.setattr(throttling, 'coordination_cache', cache)
    monkeypatch.setattr(throttling.PrincipalScopedRateThrottle, 'cache', cache)
    monkeypatch.setattr(throttling.TenantScopedRateThrottle, 'cache', cache)
    return cache


def make_tenant(slug):
    return create_tenant_with_operator_key(
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


def test_web_research_output_schema_is_strict_for_openai():
    """Every object property is required; empty arrays represent missing sections."""
    def assert_strict_object(schema):
        if schema.get('type') == 'object':
            properties = schema.get('properties', {})
            assert set(schema.get('required', [])) == set(properties)
            for child in properties.values():
                assert_strict_object(child)
        if schema.get('type') == 'array':
            assert_strict_object(schema['items'])

    assert_strict_object(WEB_RESEARCH_OUTPUT_SCHEMA)


@pytest.mark.django_db
def test_market_research_requires_uuid_and_replays_canonical_terminal_run(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('market-idempotency')
    product = make_product(tenant)
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')
    missing = Client(HTTP_AUTHORIZATION=(
        f'Bearer {create_operator_key(tenant, name="Missing UUID key")}'
    )).post(
        f'/api/v1/products/{product.pk}/market-research/',
        data={},
        content_type='application/json',
    )
    key = str(uuid.uuid4())
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/market-research/',
                data={'idempotency_key': key, 'force': True},
                content_type='application/json',
            )
    run = WebResearchRun.objects.get(
        product=product, purpose=WebResearchRun.Purpose.PRICING,
    )
    run.status = WebResearchRun.Status.COMPLETED
    run.save(update_fields=['status', 'updated_at'])
    retry = client.post(
        f'/api/v1/products/{product.pk}/market-research/',
        data={'idempotency_key': key, 'force': True},
        content_type='application/json',
    )
    conflict = client.post(
        f'/api/v1/products/{product.pk}/market-research/',
        data={'idempotency_key': key, 'force': False},
        content_type='application/json',
    )

    assert missing.status_code == 400
    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()['data']['id'] == run.pk
    assert retry.json()['data']['status'] == WebResearchRun.Status.COMPLETED
    assert conflict.status_code == 409
    assert conflict.json()['code'] == 'idempotency_conflict'
    assert PaidIngressIntent.objects.filter(
        tenant=tenant, operation='product-market-research',
    ).count() == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_market_research_retries_create_one_run_budget_and_dispatch():
    from apps.core.models import TenantDailyPaidUsage

    tenant, api_key = make_tenant('market-concurrent-idempotency')
    product = make_product(tenant)
    key = str(uuid.uuid4())

    def submit():
        close_old_connections()
        try:
            return Client(HTTP_AUTHORIZATION=f'Bearer {api_key}').post(
                f'/api/v1/products/{product.pk}/market-research/',
                data={'idempotency_key': key, 'force': True},
                content_type='application/json',
            ).status_code
        finally:
            close_old_connections()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _index: submit(), range(2)))

    assert statuses == [201, 201]
    assert WebResearchRun.objects.filter(
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
    ).count() == 1
    assert PaidIngressIntent.objects.filter(
        tenant=tenant,
        operation='product-market-research',
    ).count() == 1
    assert TenantDailyPaidUsage.objects.get(
        tenant=tenant,
        scope='web-research-starts',
    ).units == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1


def test_price_difference_describes_subject_relative_to_reference():
    cheaper = _difference(Decimal('4220'), Decimal('5092'))
    dearer = _difference(Decimal('4220'), Decimal('3637'))

    assert cheaper == {'amount': '-872.00', 'percent': '-17.1', 'direction': 'below'}
    assert dearer == {'amount': '583.00', 'percent': '16.0', 'direction': 'above'}


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
def test_custom_region_supports_non_cis_countries_with_localized_queries():
    tenant, _ = make_tenant('geo-custom-world')
    settings = TenantWebResearchSettings.objects.create(
        tenant=tenant,
        region_preset=TenantWebResearchSettings.RegionPreset.CUSTOM,
        country_codes=['DE', 'TR'],
    )

    contexts = build_search_contexts(settings, purpose=WebResearchRun.Purpose.PRICING)

    assert [context.country_code for context in contexts] == ['DE', 'TR']
    assert localize_query('BREMBO P50136', contexts[0]).endswith('Германия')
    assert localize_query('BREMBO P50136', contexts[1]).endswith('Турция')


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
    assert comparison['statistics']['listing_vs_base']['percent'] == '11.1'
    assert comparison['statistics']['median_vs_base']['percent'] == '-16.7'
    assert comparison['internet_offers'][1]['difference_from_base']['percent'] == '-16.7'
    assert any('не участвуют' in warning for warning in comparison['warnings'])


@pytest.mark.django_db
def test_settings_are_tenant_scoped_and_operator_cannot_update():
    tenant_a, _ = make_tenant('settings-a')
    tenant_b, _ = make_tenant('settings-b')
    TenantWebResearchSettings.objects.create(
        tenant=tenant_b, region_preset='worldwide', price_ttl_hours=72,
    )
    owner_client = make_owner_client(tenant_a)

    response = owner_client.put(
        '/api/v1/web-research/settings/',
        data={'region_preset': 'russia', 'price_ttl_hours': 12},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert tenant_a.web_research_settings.price_ttl_hours == 12
    assert tenant_b.web_research_settings.price_ttl_hours == 72

    geography = owner_client.put(
        '/api/v1/web-research/settings/',
        data={'region_preset': 'custom', 'country_codes': ['RU', 'DE', 'TR']},
        content_type='application/json',
    )
    reloaded = owner_client.get('/api/v1/web-research/settings/')

    assert geography.status_code == 200
    assert reloaded.json()['data']['region_preset'] == 'custom'
    assert reloaded.json()['data']['country_codes'] == ['RU', 'DE', 'TR']

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

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/market-research/',
                data={'idempotency_key': str(uuid.uuid4())},
                content_type='application/json',
            )
        second = client.post(
            f'/api/v1/products/{product.pk}/market-research/',
            data={'idempotency_key': str(uuid.uuid4())},
            content_type='application/json',
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert WebResearchRun.objects.filter(
        product=product, purpose=WebResearchRun.Purpose.PRICING,
    ).count() == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1


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
