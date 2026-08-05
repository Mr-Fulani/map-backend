from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from apps.products.models import Product, ProductEnrichmentFact, VehicleFitment
from apps.tenants.services import TenantService
from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun, WebSearchAttempt,
)
from apps.web_research.services import (
    WebResearchAgent, WebResearchService, build_research_queries, enrichment_coverage,
)
from apps.web_research.providers.base import WebSearchProviderError, WebSearchResult
from apps.web_research.routing import SearchProviderCandidate


def make_tenant(slug):
    tenant, api_key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant, api_key


def make_product(tenant, **overrides):
    data = {
        'tenant': tenant,
        'article': 'OEM0099FONR',
        'brand': '',
        'name': 'Фонарь правый внешний Kia Optima 4 JF (2016-2020)',
        'category_1c': 'Автосвет',
        'price': Decimal('0'),
    }
    data.update(overrides)
    return Product.objects.create(**data)


@pytest.mark.django_db
def test_internal_article_query_uses_product_context():
    tenant, _ = make_tenant('web-query')
    product = make_product(tenant)

    queries = build_research_queries(product)

    assert queries
    assert 'OEM0099FONR' not in queries[0]
    assert 'Kia Optima' in queries[0]


@pytest.mark.django_db
def test_sparse_product_has_low_enrichment_coverage():
    tenant, _ = make_tenant('web-coverage')
    product = make_product(tenant)

    coverage = enrichment_coverage(product)

    assert coverage['score'] < coverage['threshold']
    assert {'brand', 'oem_or_cross_codes', 'fitments'} <= set(coverage['missing'])


@pytest.mark.django_db
def test_extracted_claims_are_saved_for_review_with_evidence():
    tenant, _ = make_tenant('web-save')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    evidence = [
        WebResearchEvidence.objects.create(
            run=run,
            query='Kia Optima фонарь',
            rank=1,
            title='Kia Optima JF фонарь',
            url='https://one.example.com/part',
            domain='one.example.com',
            snippet='Фонарь OEM 92402D4000 для Kia Optima JF',
        ),
        WebResearchEvidence.objects.create(
            run=run,
            query='Kia Optima фонарь',
            rank=2,
            title='Каталог Kia',
            url='https://two.example.com/part',
            domain='two.example.com',
            snippet='92402D4000 Kia Optima JF 2016-2020',
        ),
    ]
    extracted = {
        'brand': 'OEM',
        'brand_evidence_ids': [evidence[0].pk],
        'brand_confidence': 0.9,
        'cross_codes': [{
            'manufacturer': 'KIA',
            'code': '92402D4000',
            'code_type': 'OEM',
            'evidence_ids': [evidence[0].pk, evidence[1].pk],
            'confidence': 0.95,
        }],
        'fitments': [{
            'make': 'Kia',
            'model': 'Optima',
            'generation': 'JF',
            'date_from': '2016',
            'date_to': '2020',
            'modification': '',
            'engine_code': '',
            'power_hp': None,
            'evidence_ids': [evidence[0].pk, evidence[1].pk],
            'confidence': 0.9,
        }],
        'facts': [],
    }

    claims = WebResearchService._save_extracted_claims(run, extracted, evidence)

    assert len(claims) == 3
    assert ProductEnrichmentFact.objects.filter(
        product=product,
        fact_type=ProductEnrichmentFact.FactType.BRAND,
        needs_review=True,
    ).exists()
    assert ProductEnrichmentFact.objects.filter(
        product=product,
        fact_type=ProductEnrichmentFact.FactType.OEM,
        needs_review=True,
    ).exists()
    fitment = VehicleFitment.objects.get(product=product, source_id='web_research')
    assert fitment.needs_review is True
    assert fitment.confidence == 0.7
    assert WebResearchClaim.objects.get(claim_type='brand').confidence == 0.55


def test_agent_rejects_incomplete_json():
    with pytest.raises(Exception, match='неполную структуру'):
        WebResearchAgent._parse_response('{"brand": "KIA"}')


def test_agent_accepts_fenced_json():
    payload = {
        'brand': '',
        'brand_evidence_ids': [],
        'cross_codes': [],
        'fitments': [],
        'facts': [],
    }
    parsed = WebResearchAgent._parse_response(
        f'```json\n{__import__("json").dumps(payload)}\n```',
    )
    assert parsed['fitments'] == []


def test_agent_rejects_invalid_nested_evidence_types():
    payload = {
        'brand': '',
        'brand_evidence_ids': [],
        'cross_codes': ['not-an-object'],
        'fitments': [],
        'facts': [],
    }

    with pytest.raises(Exception, match='cross_codes'):
        WebResearchAgent._parse_response(__import__('json').dumps(payload))


@pytest.mark.django_db
def test_generation_waits_for_review_and_continues_without_claims():
    tenant, _ = make_tenant('web-generate-gate')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        generate_after=True,
        status=WebResearchRun.Status.NEED_REVIEW,
    )

    with patch('apps.ai_agent.tasks.generate_description_task.delay') as delay:
        WebResearchService._generate_if_unblocked(run)
        delay.assert_not_called()

        run.status = WebResearchRun.Status.NO_RESULTS
        WebResearchService._generate_if_unblocked(run)
        delay.assert_called_once_with(product.pk)


@pytest.mark.django_db
def test_execute_runs_search_and_saves_grounded_claims():
    tenant, _ = make_tenant('web-execute')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    provider = Mock(provider_id='test_search')
    provider.search.return_value = [WebSearchResult(
        title='Kia Optima JF фонарь',
        url='https://catalog.example.com/kia-optima-light',
        snippet='Правый внешний фонарь для Kia Optima JF 2016-2020',
        rank=1,
    )]
    extracted = {
        'brand': '',
        'brand_evidence_ids': [],
        'brand_confidence': 0,
        'cross_codes': [],
        'fitments': [{
            'make': 'Kia',
            'model': 'Optima',
            'generation': 'JF',
            'date_from': '2016',
            'date_to': '2020',
            'modification': '',
            'engine_code': '',
            'power_hp': None,
            'evidence_ids': [],
            'confidence': 0.8,
        }],
        'facts': [],
    }

    def add_evidence_id(_run, evidence):
        extracted['fitments'][0]['evidence_ids'] = [evidence[0].pk]
        return extracted, SimpleNamespace(provider='openai', external_id='test-model')

    with (
        patch(
            'apps.web_research.services.search_provider_candidates',
            return_value=[SearchProviderCandidate(provider)],
        ),
        patch.object(WebResearchAgent, 'extract', side_effect=add_evidence_id),
    ):
        result = WebResearchService.execute(run.pk)

    assert result.status == WebResearchRun.Status.NEED_REVIEW
    assert result.result_count == 1
    assert result.claim_count == 1
    assert result.search_provider == 'test_search'
    assert VehicleFitment.objects.get(product=product).needs_review is True


@pytest.mark.django_db
def test_search_falls_back_and_audits_each_provider_attempt():
    tenant, _ = make_tenant('web-provider-fallback')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    first = Mock(provider_id='brave')
    first.search.side_effect = WebSearchProviderError(
        'rate limited', retryable=True, code='http_429',
    )
    second = Mock(provider_id='tavily')
    second.search.return_value = [WebSearchResult(
        title='Kia Optima lamp',
        url='https://parts.example.com/lamp',
        snippet='OEM 92402D4000',
        rank=1,
    )]
    extracted = {
        'brand': '', 'brand_evidence_ids': [], 'brand_confidence': 0,
        'cross_codes': [], 'fitments': [], 'facts': [],
    }

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(first), SearchProviderCandidate(second)],
    ), patch.object(
        WebResearchAgent, 'extract',
        return_value=(extracted, SimpleNamespace(provider='openai', external_id='test-model')),
    ):
        result = WebResearchService.execute(run.pk)

    assert result.status == WebResearchRun.Status.NO_RESULTS
    assert result.search_provider == 'tavily'
    assert list(run.search_attempts.values_list('provider_id', 'status')) == [
        ('brave', WebSearchAttempt.Status.FAILED),
        ('tavily', WebSearchAttempt.Status.SUCCESS),
    ]


@pytest.mark.django_db
def test_last_reviewed_claim_completes_research_run():
    tenant, _ = make_tenant('web-review-complete')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(
        tenant=tenant, product=product, status=WebResearchRun.Status.NEED_REVIEW,
    )
    fact = ProductEnrichmentFact.objects.create(
        tenant=tenant,
        product=product,
        source_id='web_research',
        fact_type=ProductEnrichmentFact.FactType.TECHNICAL,
        name='position',
        value='right',
        needs_review=False,
        review_status='approved',
    )
    WebResearchClaim.objects.create(
        run=run,
        claim_type=WebResearchClaim.ClaimType.FACT,
        payload={'name': 'position', 'value': 'right'},
        saved_model=fact._meta.label_lower,
        saved_record_id=fact.pk,
    )

    WebResearchService.record_claim_review(fact, 'approved')

    run.refresh_from_db()
    assert run.status == WebResearchRun.Status.COMPLETED
    assert run.claims.get().review_status == WebResearchClaim.ReviewStatus.APPROVED
