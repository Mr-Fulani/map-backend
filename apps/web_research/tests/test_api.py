from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.test import Client, override_settings

from apps.products.models import Product
from apps.tenants.tests.auth import (
    create_operator_key,
    create_tenant_with_operator_key,
)
from apps.web_research.models import WebResearchRun


@pytest.fixture(autouse=True)
def local_expensive_start_cache(monkeypatch, request):
    """Exercise start limits without requiring the development Redis host."""
    from apps.core import throttling

    cache = LocMemCache(f'web-research-start-{request.node.nodeid}', {})
    monkeypatch.setattr(throttling, 'coordination_cache', cache)
    monkeypatch.setattr(throttling.PrincipalScopedRateThrottle, 'cache', cache)
    monkeypatch.setattr(throttling.TenantScopedRateThrottle, 'cache', cache)
    return cache


def make_tenant(slug):
    tenant, api_key = create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    tenant.catalog_domain = 'auto_parts'
    tenant.save(update_fields=['catalog_domain'])
    from apps.products.services import ProductCategorySeedService
    ProductCategorySeedService.enable_tenant_catalog_domain(tenant, 'auto_parts')
    return tenant, api_key


def make_product(tenant, article='OEM0099FONR'):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        name='Фонарь правый внешний Kia Optima JF',
        category_1c='Автосвет',
        price=Decimal('0'),
    )


@pytest.mark.django_db
def test_tenant_can_start_manual_web_research(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('web-api')
    product = make_product(tenant)
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    with patch('apps.web_research.tasks.run_web_research.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/web-research/',
                content_type='application/json',
            )

    assert response.status_code == 201
    run = WebResearchRun.objects.get(product=product)
    assert run.trigger == WebResearchRun.Trigger.MANUAL
    delay.assert_called_once_with(run.pk)


@pytest.mark.django_db
@pytest.mark.parametrize('missing_scope', [
    'research:run',
    'catalog:write',
    'listings:write',
    'ai:run',
])
def test_generate_after_requires_full_effect_scope(
    missing_scope,
    django_capture_on_commit_callbacks,
):
    tenant, _ = make_tenant('web-ai-scope')
    product = make_product(tenant)
    required_scopes = {
        'research:run', 'catalog:write', 'listings:write', 'ai:run',
    }
    insufficient = create_operator_key(
        tenant,
        scopes=sorted(required_scopes - {missing_scope}),
        name=f'Missing {missing_scope}',
    )
    sufficient = create_operator_key(
        tenant,
        scopes=sorted(required_scopes),
        name='Full research workflow',
    )

    denied = Client(HTTP_AUTHORIZATION=f'Bearer {insufficient}').post(
        f'/api/v1/products/{product.pk}/web-research/',
        data={'generate_after': True},
        content_type='application/json',
    )
    with patch('apps.web_research.tasks.run_web_research.delay'):
        with django_capture_on_commit_callbacks(execute=True):
            allowed = Client(
                HTTP_AUTHORIZATION=f'Bearer {sufficient}',
            ).post(
                f'/api/v1/products/{product.pk}/web-research/',
                data={'generate_after': True},
                content_type='application/json',
            )

    assert denied.status_code == 403
    assert allowed.status_code == 201
    assert WebResearchRun.objects.get(product=product).generate_after is True


@pytest.mark.django_db
def test_web_research_requires_ai_scope_even_without_generation():
    tenant, _ = make_tenant('web-ai-base-scope')
    product = make_product(tenant)
    api_key = create_operator_key(
        tenant,
        scopes=['research:run', 'catalog:write'],
        name='No AI scope',
    )

    response = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}').post(
        f'/api/v1/products/{product.pk}/web-research/',
        content_type='application/json',
    )

    assert response.status_code == 403
    assert not WebResearchRun.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_generate_after_false_string_is_not_treated_as_true(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('web-false-boolean')
    product = make_product(tenant)
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    with patch('apps.web_research.tasks.run_web_research.delay'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/web-research/',
                data={'generate_after': 'false'},
                content_type='application/json',
            )

    assert response.status_code == 201
    assert WebResearchRun.objects.get(product=product).generate_after is False


@pytest.mark.django_db
@override_settings(WEB_RESEARCH_TENANT_DAILY_STARTS=1)
def test_daily_budget_is_shared_by_enrichment_and_market_research(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('web-shared-budget')
    enrichment_product = make_product(tenant, article='OEM-ENRICHMENT')
    pricing_product = make_product(tenant, article='OEM-PRICING')
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    with patch('apps.web_research.tasks.run_web_research.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{enrichment_product.pk}/web-research/',
                data={}, content_type='application/json',
            )
        with django_capture_on_commit_callbacks(execute=True):
            second = client.post(
                f'/api/v1/products/{pricing_product.pk}/market-research/',
                data={}, content_type='application/json',
            )

    assert first.status_code == 201
    assert second.status_code == 429
    assert WebResearchRun.objects.filter(
        tenant=tenant,
        purpose=WebResearchRun.Purpose.ENRICHMENT,
    ).count() == 1
    assert not WebResearchRun.objects.filter(
        tenant=tenant,
        purpose=WebResearchRun.Purpose.PRICING,
    ).exists()
    delay.assert_called_once()


@pytest.mark.django_db
def test_web_research_run_is_tenant_scoped():
    tenant_a, api_key = make_tenant('web-owner')
    tenant_b, _ = make_tenant('web-other')
    product_b = make_product(tenant_b)
    run = WebResearchRun.objects.create(tenant=tenant_b, product=product_b)
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    response = client.get(f'/api/v1/web-research/runs/{run.pk}/')

    assert response.status_code == 404
    assert tenant_a.web_research_runs.count() == 0


@pytest.mark.django_db
def test_dashboard_run_list_is_tenant_scoped_and_has_summary():
    tenant_a, api_key = make_tenant('web-list-owner')
    tenant_b, _ = make_tenant('web-list-other')
    product_a = make_product(tenant_a)
    product_b = make_product(tenant_b)
    own_run = WebResearchRun.objects.create(
        tenant=tenant_a,
        product=product_a,
        status=WebResearchRun.Status.NEED_REVIEW,
        result_count=3,
        claim_count=2,
    )
    WebResearchRun.objects.create(
        tenant=tenant_b,
        product=product_b,
        status=WebResearchRun.Status.FAILED,
    )
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    response = client.get('/api/v1/web-research/runs/?status=need_review')

    assert response.status_code == 200
    body = response.json()
    assert [item['id'] for item in body['data']] == [own_run.pk]
    assert body['data'][0]['product_article'] == product_a.article
    assert body['summary'] == {
        'total': 1,
        'active': 0,
        'need_review': 1,
        'failed': 0,
    }


@pytest.mark.django_db
@override_settings(BRAVE_SEARCH_API_KEY='server-secret', TAVILY_API_KEY='')
def test_tenant_provider_status_never_exposes_credentials():
    _, api_key = make_tenant('web-provider-status')
    client = Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')

    response = client.get('/api/v1/web-research/providers/')

    assert response.status_code == 200
    body = response.json()['data']
    assert body['mode'] == 'automatic'
    assert body['providers'] == [{
        'provider_id': 'brave',
        'display_name': 'Brave Search',
        'available': True,
    }]
    assert 'server-secret' not in response.content.decode()
