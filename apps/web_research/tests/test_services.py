from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import StringIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection

from apps.ai_agent.models import AIProviderOperation, AITaskType
from apps.ai_agent.providers import AIProviderError, AIProviderResult
from apps.ai_agent.reconciliation import (
    begin_ai_provider_operation, mark_ai_provider_network_started,
    settle_ai_provider_operation,
)
from apps.core.models import BackgroundJobDispatch
from apps.core.models import TenantDailyPaidUsage
from apps.core.dispatch import SafeRetryableDispatchError
from apps.products.models import Product, ProductEnrichmentFact, VehicleFitment
from apps.tenants.services import TenantService
from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun, WebSearchAttempt,
    WebSearchWorkflow,
)
from apps.web_research.accounting import (
    deterministic_web_search_call_key,
    fingerprint_web_search_request,
)
from apps.web_research.services import (
    WebResearchAgent, WebResearchReconciliationRequired, WebResearchService,
    WebResearchTerminalSearchFailure, WebResearchUnavailable,
    WebSearchOutcomeUncertain,
    build_research_queries, enrichment_coverage,
)
from apps.web_research.providers.base import WebSearchProviderError, WebSearchResult
from apps.web_research.routing import SearchProviderCandidate
from apps.web_research.search_context import SearchContext


@pytest.fixture(autouse=True)
def web_search_checkpoint_key(settings):
    key = Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = key
    settings.FIELD_ENCRYPTION_KEYS = [key]
    settings.WEB_SEARCH_CHECKPOINT_MAX_BYTES = 1024 * 1024
    settings.WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES = 128 * 1024


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


def persist_mock_ai_result(run, extracted, *, provider='openai', model='test-model'):
    operation = begin_ai_provider_operation(
        tenant=run.tenant,
        task_type=AITaskType.WEB_RESEARCH,
        provider=provider,
        model_id=model,
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(run.pk),
    )
    mark_ai_provider_network_started(operation.pk)
    operation, _ = settle_ai_provider_operation(
        operation.pk,
        actual_amount=Decimal('1'),
        validated_result=extracted,
        apply_required=True,
    )
    result = dict(extracted)
    result['_provider_operation_id'] = str(operation.pk)
    return result, SimpleNamespace(provider=provider, external_id=model)


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
def test_create_run_daily_budget_rolls_back_with_outer_transaction():
    from django.db import transaction

    tenant, _ = make_tenant('web-budget-rollback')
    product = make_product(tenant)

    with pytest.raises(RuntimeError, match='abort outer transaction'):
        with transaction.atomic():
            WebResearchService.create_run(
                product,
                trigger=WebResearchRun.Trigger.MANUAL,
            )
            raise RuntimeError('abort outer transaction')

    assert not WebResearchRun.objects.filter(product=product).exists()
    assert not TenantDailyPaidUsage.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_old_unapplied_ai_operation_blocks_distinct_new_run_until_released():
    tenant, _ = make_tenant('web-product-ai-fence')
    product = make_product(tenant)
    old_run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.COMPLETED,
    )
    operation = AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.WEB_RESEARCH,
        provider='openai',
        model_id='test-model',
        reservation_key='web-product-ai-fence:reservation',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(old_run.pk),
        status=AIProviderOperation.Status.SETTLED,
        charged_amount=Decimal('1'),
        apply_state=AIProviderOperation.ApplyState.PENDING,
    )

    with pytest.raises(WebResearchReconciliationRequired):
        WebResearchService.create_run(
            product,
            trigger=WebResearchRun.Trigger.MANUAL,
            origin_key='new-distinct-request',
        )

    assert WebResearchRun.objects.filter(product=product).count() == 1
    assert not TenantDailyPaidUsage.objects.filter(tenant=tenant).exists()
    operation.apply_state = AIProviderOperation.ApplyState.APPLIED
    operation.save(update_fields=['apply_state', 'updated_at'])
    new_run, created = WebResearchService.create_run(
        product,
        trigger=WebResearchRun.Trigger.MANUAL,
        origin_key='new-distinct-request',
    )
    assert created is True
    assert new_run.pk != old_run.pk


@pytest.mark.django_db
def test_malformed_unresolved_ai_reference_fails_closed_without_history_scan():
    tenant, _ = make_tenant('web-malformed-ai-fence')
    product = make_product(tenant)
    AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.WEB_RESEARCH,
        provider='openai',
        model_id='test-model',
        reservation_key='web-malformed-ai-fence:reservation',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference='malformed-audit-owner',
        status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        apply_state=AIProviderOperation.ApplyState.PENDING,
    )

    with pytest.raises(WebResearchReconciliationRequired):
        WebResearchService.create_run(
            product,
            trigger=WebResearchRun.Trigger.MANUAL,
            origin_key='malformed-reference-request',
        )

    assert not WebResearchRun.objects.filter(product=product).exists()
    assert not TenantDailyPaidUsage.objects.filter(tenant=tenant).exists()


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

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        WebResearchService._generate_if_unblocked(run)
        assert not BackgroundJobDispatch.objects.exists()

        run.status = WebResearchRun.Status.NO_RESULTS
        WebResearchService._generate_if_unblocked(run)
        assert BackgroundJobDispatch.objects.filter(
            task_name='apps.ai_agent.tasks.generate_description_task',
            args=[product.pk],
        ).count() == 1


@pytest.mark.django_db
def test_execute_runs_search_and_saves_grounded_claims():
    tenant, _ = make_tenant('web-execute')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    # The immutable plan only accepts registered provider identities; use a
    # real registry key while keeping the network implementation mocked.
    provider = Mock(provider_id='brave')
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
        return persist_mock_ai_result(_run, extracted)

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
    assert result.search_provider == 'brave'
    assert VehicleFitment.objects.get(product=product).needs_review is True


@pytest.mark.django_db
def test_search_falls_back_and_audits_each_provider_attempt():
    tenant, _ = make_tenant('web-provider-fallback')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    first = Mock(provider_id='brave')
    first.search.side_effect = WebSearchProviderError(
        'authentication rejected', retryable=False, code='http_403',
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
        side_effect=lambda research_run, _evidence: persist_mock_ai_result(
            research_run, extracted,
        ),
    ):
        result = WebResearchService.execute(run.pk)

    assert result.status == WebResearchRun.Status.NO_RESULTS
    assert result.search_provider == 'tavily'
    assert list(run.search_attempts.values_list('provider_id', 'status')) == [
        ('brave', WebSearchAttempt.Status.FAILED),
        ('tavily', WebSearchAttempt.Status.SUCCESS),
    ]


@pytest.mark.django_db
def test_all_safe_provider_failures_are_acknowledged_without_stranding_fence():
    tenant, _ = make_tenant('web-all-safe-failures')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    first = Mock(provider_id='brave')
    first.search.side_effect = WebSearchProviderError(
        'request rejected before processing',
        retryable=False,
        code='http_422',
    )
    second = Mock(provider_id='tavily')
    second.search.side_effect = WebSearchProviderError(
        'provider disabled before send',
        retryable=False,
        code='pre_send_failure',
    )

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(first), SearchProviderCandidate(second)],
    ), pytest.raises(
        WebResearchTerminalSearchFailure,
        match='disabled before send',
    ):
        WebResearchService.execute(run.pk)

    workflow = WebSearchWorkflow.objects.get(run=run)
    attempts = list(workflow.attempts.order_by('pk'))
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert len(attempts) == 2
    assert all(
        item.apply_state == WebSearchAttempt.ApplyState.APPLIED
        for item in attempts
    )
    assert all(
        item.reconciliation_state
        == WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        for item in attempts
    )
    first.search.assert_called_once()
    second.search.assert_called_once()

    # A terminal run replay is a no-op and cannot re-enter either provider.
    WebResearchService.execute(run.pk)
    first.search.assert_called_once()
    second.search.assert_called_once()


@pytest.mark.django_db
def test_safe_failure_then_empty_result_acknowledges_exact_recorded_plan():
    tenant, _ = make_tenant('web-safe-then-empty')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    first = Mock(provider_id='brave')
    first.search.side_effect = WebSearchProviderError(
        'documented rejection', retryable=False, code='http_422',
    )
    second = Mock(provider_id='tavily')
    second.search.return_value = []

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(first), SearchProviderCandidate(second)],
    ):
        result = WebResearchService.execute(run.pk)

    assert result.status == WebResearchRun.Status.NO_RESULTS
    workflow = WebSearchWorkflow.objects.get(run=run)
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert workflow.attempts.count() == 2
    assert not workflow.attempts.exclude(
        apply_state=WebSearchAttempt.ApplyState.APPLIED,
    ).exists()
    first.search.assert_called_once()
    second.search.assert_called_once()

    WebResearchService.execute(run.pk)
    first.search.assert_called_once()
    second.search.assert_called_once()


@pytest.mark.django_db
def test_worker_kill_after_provider_checkpoint_replays_locally_and_applies_once():
    tenant, _ = make_tenant('web-checkpoint-worker-kill')
    product = make_product(tenant, article='KILL-REPLAY-1')
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
    )
    provider = Mock(provider_id='brave')
    provider.search.return_value = [WebSearchResult(
        title='Kia lamp',
        url='https://parts.example.com/kill-replay-lamp',
        snippet='KILL-REPLAY-1 12 000 RUB в наличии',
        rank=1,
    )]

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(provider)],
    ), patch(
        'apps.web_research.services.acknowledge_web_search_workflow',
        side_effect=SystemExit('simulated hard kill before domain commit'),
    ), pytest.raises(SystemExit, match='simulated hard kill'):
        WebResearchService.execute(run.pk)

    run.refresh_from_db()
    workflow = WebSearchWorkflow.objects.get(run=run)
    attempt = workflow.attempts.get()
    assert run.status == WebResearchRun.Status.RUNNING
    assert workflow.status == WebSearchWorkflow.Status.APPLY_PENDING
    assert attempt.status == WebSearchAttempt.Status.SUCCESS
    assert attempt.apply_state == WebSearchAttempt.ApplyState.PENDING
    assert not run.evidence.exists()

    # Credentials/routing are deliberately unavailable on recovery. The exact
    # encrypted checkpoint is restored before provider resolution.
    result = WebResearchService.execute(run.pk)

    provider.search.assert_called_once()
    workflow.refresh_from_db()
    attempt.refresh_from_db()
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert attempt.apply_state == WebSearchAttempt.ApplyState.APPLIED
    assert result.status in {
        WebResearchRun.Status.COMPLETED,
        WebResearchRun.Status.NEED_REVIEW,
        WebResearchRun.Status.NO_RESULTS,
    }
    assert run.evidence.count() == 1
    assert WebResearchService.execute(run.pk).pk == run.pk
    provider.search.assert_called_once()


@pytest.mark.django_db
def test_local_evidence_failure_keeps_checkpoint_replayable_without_second_call():
    tenant, _ = make_tenant('web-checkpoint-local-failure')
    product = make_product(tenant, article='LOCAL-APPLY-FAILURE-1')
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
    )
    provider = Mock(provider_id='brave')
    provider.search.return_value = [WebSearchResult(
        title='Kia lamp',
        url='https://parts.example.com/local-apply-failure',
        snippet='LOCAL-APPLY-FAILURE-1 12 000 RUB в наличии',
        rank=1,
    )]

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(provider)],
    ), patch(
        'apps.web_research.services.WebResearchEvidence.objects.get_or_create',
        side_effect=RuntimeError('evidence database temporarily unavailable'),
    ), pytest.raises(RuntimeError, match='temporarily unavailable'):
        WebResearchService.execute(run.pk)

    run.refresh_from_db()
    workflow = WebSearchWorkflow.objects.get(run=run)
    attempt = workflow.attempts.get()
    assert run.status == WebResearchRun.Status.QUEUED
    assert run.finished_at is None
    assert workflow.status == WebSearchWorkflow.Status.APPLY_PENDING
    assert attempt.status == WebSearchAttempt.Status.SUCCESS
    assert attempt.apply_state == WebSearchAttempt.ApplyState.PENDING
    assert not run.evidence.exists()

    # A repeated local apply failure still replays the same checkpoint and
    # leaves the run recoverable after the dispatch attempt budget is spent.
    with patch(
        'apps.web_research.services.WebResearchEvidence.objects.get_or_create',
        side_effect=RuntimeError('evidence database still unavailable'),
    ), pytest.raises(RuntimeError, match='still unavailable'):
        WebResearchService.execute(run.pk)
    provider.search.assert_called_once()
    run.refresh_from_db()
    assert run.status == WebResearchRun.Status.QUEUED
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.web_research.tasks.run_web_research',
        queue='part_parsing',
        args=[run.pk],
        deduplication_key=f'web-research-run:{run.pk}',
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=3,
        max_run_attempts=3,
        finished_at=run.updated_at,
    )
    with pytest.raises(CommandError, match='exactly match'):
        call_command(
            'resume_web_research_checkpoint',
            run_id=run.pk,
            confirm='wrong',
        )
    # The command must only persist/revive the durable dispatch here. Avoid a
    # real broker publish from the test's on-commit callback.
    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ):
        call_command(
            'resume_web_research_checkpoint',
            run_id=run.pk,
            confirm=str(run.pk),
            stdout=StringIO(),
        )
    dispatch.refresh_from_db()
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert dispatch.run_attempts == 0

    # No provider configuration is patched on recovery: the encrypted result
    # is restored before runtime credentials/routing are consulted.
    result = WebResearchService.execute(run.pk)

    provider.search.assert_called_once()
    workflow.refresh_from_db()
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert result.status in {
        WebResearchRun.Status.COMPLETED,
        WebResearchRun.Status.NEED_REVIEW,
        WebResearchRun.Status.NO_RESULTS,
    }
    assert run.evidence.count() == 1


@pytest.mark.django_db
def test_operator_revives_exhausted_safe_in_progress_plan_without_repaying_slot():
    tenant, _ = make_tenant('web-safe-plan-recovery')
    product = make_product(tenant, article='SAFE-PLAN-RECOVERY-1')
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
        status=WebResearchRun.Status.QUEUED,
    )
    context = SearchContext(
        country_code='',
        market_intent='pricing',
        strict_region=False,
        result_limit=5,
    )
    first = Mock(provider_id='brave')
    second = Mock(provider_id='tavily')
    second.search.return_value = []
    candidates = [
        SearchProviderCandidate(first),
        SearchProviderCandidate(second),
    ]
    plan = WebResearchService._build_search_workflow_plan(
        run,
        ['SAFE-PLAN-RECOVERY-1'],
        [context],
        candidates,
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        run=run,
        operation='web_research',
        domain_reference=f'product:{product.pk}:purpose:pricing',
        workflow_key=f'web-research-run:{run.pk}',
        input_fingerprint=fingerprint_web_search_request(plan),
        input_snapshot=plan,
        status=WebSearchWorkflow.Status.IN_PROGRESS,
    )
    slot = plan['slots'][0]
    first_request = {
        'provider_id': 'brave',
        'query': slot['query'],
        'count': slot['count'],
        'context': slot['context'],
    }
    first_attempt = WebSearchAttempt.objects.create(
        tenant=tenant,
        workflow=workflow,
        run=run,
        provider_id='brave',
        operation='web_research',
        call_kind='text',
        domain_reference=workflow.domain_reference,
        call_key=deterministic_web_search_call_key(
            provider_id='brave',
            call_kind='text',
            slot=f"{slot['slot']}:provider:0",
        ),
        request_fingerprint=fingerprint_web_search_request(first_request),
        query=slot['query'],
        status=WebSearchAttempt.Status.FAILED,
        error_code='http_422',
        error_message='documented provider rejection',
        reconciliation_state=(
            WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        ),
        apply_state=WebSearchAttempt.ApplyState.PENDING,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.web_research.tasks.run_web_research',
        queue='part_parsing',
        args=[run.pk],
        deduplication_key=f'web-research-run:{run.pk}',
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=3,
        max_run_attempts=3,
        finished_at=run.updated_at,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        call_command(
            'resume_web_research_checkpoint',
            run_id=run.pk,
            confirm=str(run.pk),
            stdout=StringIO(),
        )
    dispatch.refresh_from_db()
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert dispatch.run_attempts == 0

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=candidates,
    ):
        result = WebResearchService.execute(run.pk)

    first.search.assert_not_called()
    second.search.assert_called_once()
    first_attempt.refresh_from_db()
    workflow.refresh_from_db()
    assert first_attempt.apply_state == WebSearchAttempt.ApplyState.APPLIED
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert result.status == WebResearchRun.Status.NO_RESULTS


@pytest.mark.django_db
def test_unresolved_ai_never_marks_dispatch_succeeded_and_released_run_resumes():
    tenant, _ = make_tenant('web-ai-reconcile-dispatch')
    product = make_product(tenant, article='AI-RECONCILE-DISPATCH-1')
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.ENRICHMENT,
        status=WebResearchRun.Status.RUNNING,
    )
    evidence = WebResearchEvidence.objects.create(
        run=run,
        query='AI-RECONCILE-DISPATCH-1',
        rank=1,
        provider_id='brave',
        title='Paid search checkpoint',
        url='https://parts.example.com/ai-reconcile-dispatch',
        domain='parts.example.com',
        snippet='AI-RECONCILE-DISPATCH-1 12 000 RUB',
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=None,
        run=run,
        operation='web_research',
        domain_reference=f'product:{product.pk}:purpose:pricing',
        workflow_key=f'web-research-run:{run.pk}',
        input_fingerprint='c' * 64,
        input_snapshot={'version': 1, 'providers': [], 'slots': []},
        status=WebSearchWorkflow.Status.APPLIED,
    )
    WebSearchAttempt.objects.create(
        tenant=tenant,
        workflow=workflow,
        run=run,
        provider_id='brave',
        operation='web_research',
        call_kind='text',
        domain_reference=workflow.domain_reference,
        call_key='brave:text:slot:0',
        request_fingerprint='d' * 64,
        query=evidence.query,
        status=WebSearchAttempt.Status.SUCCESS,
        checkpoint_enc=b'encrypted-search-checkpoint',
        reconciliation_state=(
            WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        ),
        apply_state=WebSearchAttempt.ApplyState.APPLIED,
    )
    operation = AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.WEB_RESEARCH,
        provider='openai',
        model_id='test-model',
        reservation_key='web-ai-reconcile-dispatch:reservation',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(run.pk),
        status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.web_research.tasks.run_web_research',
        queue='part_parsing',
        args=[run.pk],
        deduplication_key=f'web-research-run:{run.pk}',
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=1,
        max_run_attempts=3,
    )

    from apps.core.dispatch import execute_claimed_dispatch

    target = SimpleNamespace(
        run=lambda run_id: WebResearchService.execute(run_id),
    )
    with patch('apps.core.dispatch._registered_task', return_value=target):
        result = execute_claimed_dispatch(dispatch)
    dispatch.refresh_from_db()
    assert result['status'] == 'failed'
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert dispatch.last_error

    operation.status = AIProviderOperation.Status.RELEASED
    operation.charged_amount = Decimal('0')
    operation.apply_state = AIProviderOperation.ApplyState.NOT_REQUIRED
    operation.save(update_fields=[
        'status', 'charged_amount', 'apply_state', 'updated_at',
    ])
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        call_command(
            'resume_web_research_checkpoint',
            run_id=run.pk,
            confirm=str(run.pk),
            stdout=StringIO(),
        )
    dispatch.refresh_from_db()
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert dispatch.run_attempts == 0

    with patch(
        'apps.web_research.services.search_provider_candidates',
        side_effect=AssertionError('search provider must not be resolved'),
    ):
        resumed = WebResearchService.execute(run.pk)

    assert resumed.status == WebResearchRun.Status.NO_RESULTS


@pytest.mark.django_db
def test_failure_after_search_ack_resumes_local_phase_without_second_provider_call():
    tenant, _ = make_tenant('web-post-ack-local-resume')
    product = make_product(tenant, article='POST-ACK-RESUME-1')
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
    )
    provider = Mock(provider_id='brave')
    provider.search.return_value = [WebSearchResult(
        title='Kia lamp',
        url='https://parts.example.com/post-ack-resume',
        snippet='POST-ACK-RESUME-1 12 000 RUB в наличии',
        rank=1,
    )]

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(provider)],
    ), patch(
        'apps.web_research.services.save_deterministic_offers',
        side_effect=RuntimeError('pricing database temporarily unavailable'),
    ), pytest.raises(RuntimeError, match='pricing database'):
        WebResearchService.execute(run.pk)

    run.refresh_from_db()
    workflow = WebSearchWorkflow.objects.get(run=run)
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert run.status == WebResearchRun.Status.QUEUED
    assert run.evidence.count() == 1
    with pytest.raises(WebResearchReconciliationRequired):
        WebResearchService.create_run(
            product,
            trigger=WebResearchRun.Trigger.MANUAL,
            purpose=WebResearchRun.Purpose.PRICING,
            origin_key='post-ack-distinct-request',
        )

    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.web_research.tasks.run_web_research',
        queue='part_parsing',
        args=[run.pk],
        deduplication_key=f'web-research-run:{run.pk}',
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=3,
        max_run_attempts=3,
        finished_at=run.updated_at,
    )
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        call_command(
            'resume_web_research_checkpoint',
            run_id=run.pk,
            confirm=str(run.pk),
            stdout=StringIO(),
        )
    dispatch.refresh_from_db()
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING

    # The canonical evidence is the local resume checkpoint. Runtime provider
    # routing is not consulted and no second paid search is emitted.
    with patch(
        'apps.web_research.services.search_provider_candidates',
        side_effect=AssertionError('search provider must not be resolved'),
    ), patch(
        'apps.web_research.services.save_deterministic_offers',
        return_value=[],
    ):
        result = WebResearchService.execute(run.pk)

    provider.search.assert_called_once()
    assert result.status == WebResearchRun.Status.NO_RESULTS
    assert result.finished_at is not None


@pytest.mark.django_db(transaction=True)
def test_session_owner_lock_allows_only_one_web_research_apply_executor():
    if connection.vendor != 'postgresql':
        pytest.skip('session advisory ownership is a PostgreSQL invariant')
    tenant, _ = make_tenant('web-session-owner-lock')
    product = make_product(tenant, article='SESSION-OWNER-LOCK-1')
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        purpose=WebResearchRun.Purpose.PRICING,
    )
    entered_provider = Event()
    release_provider = Event()
    provider = Mock(provider_id='brave')

    def delayed_search(*_args, **_kwargs):
        entered_provider.set()
        assert release_provider.wait(timeout=10)
        return []

    provider.search.side_effect = delayed_search

    def first_executor():
        close_old_connections()
        try:
            return WebResearchService.execute(run.pk).status
        finally:
            close_old_connections()

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(provider)],
    ), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_executor)
        assert entered_provider.wait(timeout=10)
        with pytest.raises(
            SafeRetryableDispatchError,
            match='already owned',
        ):
            WebResearchService.execute(run.pk)
        release_provider.set()
        assert future.result(timeout=10) == WebResearchRun.Status.NO_RESULTS

    provider.search.assert_called_once()
    workflow = WebSearchWorkflow.objects.get(run=run)
    assert workflow.status == WebSearchWorkflow.Status.APPLIED


@pytest.mark.django_db
def test_worker_kill_before_checkpoint_never_replays_provider_network_call():
    tenant, _ = make_tenant('web-started-worker-kill')
    product = make_product(tenant, article='STARTED-KILL-1')
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    provider = Mock(provider_id='brave')
    provider.search.side_effect = SystemExit('worker killed with HTTP in flight')

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(provider)],
    ), pytest.raises(SystemExit, match='HTTP in flight'):
        WebResearchService.execute(run.pk)

    workflow = WebSearchWorkflow.objects.get(run=run)
    attempt = workflow.attempts.get()
    assert attempt.status == WebSearchAttempt.Status.STARTED
    assert attempt.reconciliation_state == (
        WebSearchAttempt.ReconciliationState.PENDING
    )

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(provider)],
    ), pytest.raises(WebSearchOutcomeUncertain, match='fallback запрещён'):
        WebResearchService.execute(run.pk)

    provider.search.assert_called_once()
    attempt.refresh_from_db()
    workflow.refresh_from_db()
    assert attempt.status == WebSearchAttempt.Status.STARTED
    assert workflow.status == WebSearchWorkflow.Status.UNCERTAIN


@pytest.mark.django_db
def test_uncertain_search_outcome_stops_before_paid_fallback():
    tenant, _ = make_tenant('web-provider-uncertain')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    first = Mock(provider_id='brave')
    first.search.side_effect = WebSearchProviderError(
        'read timeout',
        retryable=True,
        code='connection_error',
        outcome_uncertain=True,
    )
    second = Mock(provider_id='tavily')

    with patch(
        'apps.web_research.services.search_provider_candidates',
        return_value=[SearchProviderCandidate(first), SearchProviderCandidate(second)],
    ), pytest.raises(WebResearchUnavailable, match='fallback запрещён'):
        WebResearchService.execute(run.pk)

    first.search.assert_called_once()
    second.search.assert_not_called()
    run.refresh_from_db()
    assert run.status == WebResearchRun.Status.FAILED
    attempt = run.search_attempts.get(provider_id='brave')
    assert attempt.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN
    assert attempt.error_code == 'connection_error'


@pytest.mark.django_db
def test_uncertain_research_ai_outcome_never_falls_back_or_releases_reservation():
    tenant, _ = make_tenant('web-ai-uncertain')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    evidence = [WebResearchEvidence.objects.create(
        run=run,
        query='lamp',
        rank=1,
        provider_id='brave',
        title='Kia lamp',
        url='https://parts.example.com/lamp',
        domain='parts.example.com',
        snippet='Kia Optima',
    )]
    model = Mock(provider='openai', external_id='test-model', max_output_tokens=100)
    model.estimate_credits.return_value = Decimal('1')

    with patch(
        'apps.web_research.services.LimitChecker.can_generate_ai', return_value=(True, ''),
    ), patch(
        'apps.web_research.services.AIModelRouter.candidates', return_value=[model, Mock()],
    ), patch(
        'apps.web_research.services.release_ai_provider_operation',
    ) as release, patch(
        'apps.web_research.services.call_model',
        side_effect=AIProviderError(
            'read timeout', code='connection_error', retryable=True,
            outcome_uncertain=True,
        ),
    ) as provider, patch.object(
        WebResearchAgent, '_log',
    ), pytest.raises(WebResearchUnavailable, match='fallback запрещён'):
        WebResearchAgent().extract(run, evidence)

    provider.assert_called_once()
    release.assert_not_called()
    operation = AIProviderOperation.objects.get(
        tenant=tenant,
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(run.pk),
    )
    assert operation.task_type == 'web_research'
    assert operation.status == AIProviderOperation.Status.PENDING_RECONCILIATION
    assert operation.provider_error_code == 'connection_error'
    assert operation.network_started_at is not None


@pytest.mark.django_db
def test_rate_limited_research_ai_never_falls_back_or_releases_reservation():
    tenant, _ = make_tenant('web-ai-rate-limit-uncertain')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    evidence = [WebResearchEvidence.objects.create(
        run=run,
        query='lamp',
        rank=1,
        provider_id='brave',
        title='Kia lamp',
        url='https://parts.example.com/lamp',
        domain='parts.example.com',
        snippet='Kia Optima',
    )]
    model = Mock(provider='openai', external_id='test-model', max_output_tokens=100)
    model.estimate_credits.return_value = Decimal('1')

    with patch(
        'apps.web_research.services.LimitChecker.can_generate_ai',
        return_value=(True, ''),
    ), patch(
        'apps.web_research.services.AIModelRouter.candidates',
        return_value=[model, Mock()],
    ), patch(
        'apps.web_research.services.release_ai_provider_operation',
    ) as release, patch(
        'apps.web_research.services.call_model',
        side_effect=AIProviderError(
            'rate limited',
            code='http_429',
            retryable=True,
            outcome_uncertain=True,
        ),
    ) as provider, patch.object(
        WebResearchAgent, '_log',
    ), pytest.raises(WebResearchUnavailable, match='fallback запрещён'):
        WebResearchAgent().extract(run, evidence)

    provider.assert_called_once()
    release.assert_not_called()
    operation = AIProviderOperation.objects.get(
        tenant=tenant,
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(run.pk),
    )
    assert operation.status == AIProviderOperation.Status.PENDING_RECONCILIATION
    assert operation.provider_error_code == 'http_429'


@pytest.mark.django_db
def test_rejected_research_payload_is_charged_before_model_retry():
    tenant, _ = make_tenant('web-ai-validation-paid')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    evidence = [WebResearchEvidence.objects.create(
        run=run,
        query='lamp',
        rank=1,
        provider_id='brave',
        title='Kia lamp',
        url='https://parts.example.com/lamp',
        domain='parts.example.com',
        snippet='Kia Optima',
    )]
    first = Mock(provider='openai', external_id='first-model', max_output_tokens=100)
    second = Mock(provider='anthropic', external_id='second-model', max_output_tokens=100)
    for model in (first, second):
        model.estimate_credits.return_value = Decimal('2')
        model.calculate_credits.return_value = Decimal('1')
    invalid = AIProviderResult(
        text='{"brand":"KIA"}',
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        response_model='first-model',
    )
    valid_payload = {
        'brand': '',
        'brand_evidence_ids': [],
        'cross_codes': [],
        'fitments': [],
        'facts': [],
    }
    valid = AIProviderResult(
        text=__import__('json').dumps(valid_payload),
        input_tokens=11,
        cached_input_tokens=0,
        output_tokens=6,
        response_model='second-model',
    )

    with patch(
        'apps.web_research.services.LimitChecker.can_generate_ai', return_value=(True, ''),
    ), patch(
        'apps.web_research.services.AIModelRouter.candidates', return_value=[first, second],
    ), patch(
        'apps.web_research.services.call_model', side_effect=[invalid, valid],
    ) as provider:
        parsed, selected = WebResearchAgent().extract(run, evidence)

    operation_id = parsed.pop('_provider_operation_id')
    assert parsed == {**valid_payload, 'offers': []}
    assert selected is second
    assert provider.call_count == 2
    operations = list(AIProviderOperation.objects.filter(tenant=tenant).order_by('created_at'))
    assert len(operations) == 2
    assert [operation.status for operation in operations] == [
        AIProviderOperation.Status.SETTLED,
        AIProviderOperation.Status.SETTLED,
    ]
    assert operations[0].terminal_reason == 'validation_rejected'
    assert operations[0].charged_amount == Decimal('1')
    assert str(operations[1].pk) == operation_id
    assert operations[1].apply_state == AIProviderOperation.ApplyState.PENDING
    assert tenant.ai_credit_transactions.filter(
        idempotency_key=f'{operations[0].reservation_key}:release',
    ).exists() is False
    assert tenant.ai_credit_transactions.filter(
        idempotency_key=f'{operations[0].reservation_key}:settled',
    ).count() == 1


@pytest.mark.django_db
def test_research_settlement_failure_is_held_for_reconciliation():
    tenant, _ = make_tenant('web-ai-settlement-failure')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    evidence = [WebResearchEvidence.objects.create(
        run=run,
        query='lamp',
        rank=1,
        provider_id='brave',
        title='Kia lamp',
        url='https://parts.example.com/lamp',
        domain='parts.example.com',
        snippet='Kia Optima',
    )]
    model = Mock(provider='openai', external_id='test-model', max_output_tokens=100)
    model.estimate_credits.return_value = Decimal('2')
    model.calculate_credits.return_value = Decimal('1')
    provider_result = AIProviderResult(
        text=__import__('json').dumps({
            'brand': '',
            'brand_evidence_ids': [],
            'cross_codes': [],
            'fitments': [],
            'facts': [],
        }),
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        response_model='test-model',
    )

    with patch(
        'apps.web_research.services.LimitChecker.can_generate_ai', return_value=(True, ''),
    ), patch(
        'apps.web_research.services.AIModelRouter.candidates', return_value=[model, Mock()],
    ), patch(
        'apps.web_research.services.call_model', return_value=provider_result,
    ) as provider, patch(
        'apps.web_research.services.settle_ai_provider_operation',
        side_effect=RuntimeError('database commit outcome unknown'),
    ), pytest.raises(WebResearchUnavailable, match='операция передана на сверку'):
        WebResearchAgent().extract(run, evidence)

    provider.assert_called_once()
    operation = AIProviderOperation.objects.get(tenant=tenant)
    assert operation.status == AIProviderOperation.Status.PENDING_RECONCILIATION
    assert operation.provider_error_code == 'settlement_failed'


@pytest.mark.django_db
def test_run_resumes_paid_result_after_crash_before_claim_writes():
    tenant, _ = make_tenant('web-ai-paid-result-resume')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.RUNNING,
    )
    evidence = [WebResearchEvidence.objects.create(
        run=run,
        query='lamp',
        rank=1,
        provider_id='brave',
        title='Kia lamp',
        url='https://parts.example.com/lamp',
        domain='parts.example.com',
        snippet='Kia Optima JF',
    )]
    model = Mock(provider='openai', external_id='test-model', max_output_tokens=100)
    model.estimate_credits.return_value = Decimal('2')
    model.calculate_credits.return_value = Decimal('1')
    provider_result = AIProviderResult(
        text=__import__('json').dumps({
            'brand': '',
            'brand_evidence_ids': [],
            'cross_codes': [],
            'fitments': [{
                'make': 'Kia',
                'model': 'Optima',
                'generation': 'JF',
                'date_from': '',
                'date_to': '',
                'modification': '',
                'engine_code': '',
                'power_hp': None,
                'evidence_ids': [evidence[0].pk],
                'confidence': 0.8,
            }],
            'facts': [],
        }),
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        response_model='test-model',
    )

    with patch(
        'apps.web_research.services.LimitChecker.can_generate_ai', return_value=(True, ''),
    ), patch(
        'apps.web_research.services.AIModelRouter.candidates', return_value=[model],
    ), patch(
        'apps.web_research.services.call_model', return_value=provider_result,
    ) as provider:
        extracted, _ = WebResearchAgent().extract(run, evidence)

    provider.assert_called_once()
    operation_id = extracted['_provider_operation_id']
    operation = AIProviderOperation.objects.get(pk=operation_id)
    assert operation.apply_state == AIProviderOperation.ApplyState.PENDING
    assert run.claims.count() == 0

    with patch(
        'apps.web_research.services.search_provider_candidates',
        side_effect=AssertionError('search/provider must not be replayed'),
    ) as replay:
        resumed = WebResearchService.execute(run.pk)

    replay.assert_not_called()
    operation.refresh_from_db()
    assert operation.apply_state == AIProviderOperation.ApplyState.APPLIED
    assert operation.applied_at is not None
    assert resumed.status == WebResearchRun.Status.NEED_REVIEW
    assert resumed.claim_count == 1
    assert resumed.ai_provider == 'openai'
    assert resumed.ai_model == 'test-model'
    assert VehicleFitment.objects.filter(
        product=product,
        make='Kia',
        model='Optima',
        generation='JF',
    ).count() == 1

    # Exact result application remains idempotent.
    WebResearchService.apply_ai_provider_operation(operation.pk)
    assert run.claims.count() == 1


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
