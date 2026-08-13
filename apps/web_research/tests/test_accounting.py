from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, transaction
from django.db.models.deletion import ProtectedError
from django.utils.timezone import now

from apps.products.models import Product
from apps.tenants.services import TenantService
from apps.web_research.accounting import (
    WebSearchLimitExceeded,
    WebSearchReconciliationRequired,
    WebSearchWorkflowConflict,
    acknowledge_web_search_workflow,
    acquire_web_search_workflow,
    deterministic_web_search_call_key,
    execute_recorded_web_search,
    fingerprint_web_search_request,
    reserve_web_search_attempt,
    resolve_web_search_attempt,
)
from apps.web_research.models import (
    WebResearchEvidence,
    WebResearchRun,
    WebSearchAttempt,
    WebSearchConnection,
    WebSearchWorkflow,
)
from apps.web_research.providers.base import WebSearchProviderError


@pytest.fixture(autouse=True)
def web_search_checkpoint_key(settings):
    key = Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = key
    settings.FIELD_ENCRYPTION_KEYS = [key]
    settings.WEB_SEARCH_CHECKPOINT_MAX_BYTES = 1024 * 1024
    settings.WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES = 128 * 1024


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    return tenant


def make_product(tenant, suffix='1'):
    return Product.objects.create(
        tenant=tenant,
        article=f'WF-{suffix}',
        name=f'Workflow product {suffix}',
        price='1.00',
    )


def make_workflow(
    tenant, *, key, domain, operation='web_research', product=None,
    snapshot=None, run=None,
):
    return acquire_web_search_workflow(
        tenant=tenant,
        operation=operation,
        domain_reference=domain,
        workflow_key=key,
        input_snapshot=snapshot or {'version': 1, 'key': key},
        product=product,
        run=run,
    )


def execute_call(
    workflow, *, provider_id='brave', slot='slot:0', query='part',
    call=None, connection=None, call_kind='text', request_payload=None,
    normalize_result=lambda value: value, restore_result=lambda value: value,
):
    payload = request_payload or {
        'provider_id': provider_id, 'query': query, 'slot': slot,
    }
    fingerprint = fingerprint_web_search_request(payload)
    return execute_recorded_web_search(
        workflow=workflow,
        provider=SimpleNamespace(provider_id=provider_id),
        connection=connection,
        query=query,
        call_key=deterministic_web_search_call_key(
            provider_id=provider_id,
            call_kind=call_kind,
            slot=slot,
        ),
        request_fingerprint=fingerprint,
        call_kind=call_kind,
        call=call or (lambda: []),
        normalize_result=normalize_result,
        restore_result=restore_result,
    )


@pytest.mark.django_db
def test_provider_success_checkpoint_replays_without_network_and_apply_is_atomic():
    tenant = make_tenant('search-checkpoint-replay')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    workflow = make_workflow(
        tenant,
        key=f'web-research-run:{run.pk}',
        domain=f'product:{product.pk}:purpose:enrichment',
        product=product,
        run=run,
    )
    outbound = Mock(return_value=[{'url': 'https://example.com/part'}])

    first = execute_call(workflow, call=outbound)
    replay = execute_call(workflow, call=outbound)

    assert replay.result == first.result
    assert replay.attempt_id == first.attempt_id
    assert replay.replayed is True
    outbound.assert_called_once()
    with pytest.raises(WebSearchReconciliationRequired):
        make_workflow(
            tenant,
            key='other-business-execution',
            domain=workflow.domain_reference,
            product=product,
        )

    with pytest.raises(RuntimeError):
        with transaction.atomic():
            WebResearchEvidence.objects.create(
                run=run,
                query='part',
                rank=1,
                url='https://example.com/part',
                domain='example.com',
            )
            acknowledge_web_search_workflow(
                workflow.pk,
                consumed_attempt_ids={first.attempt_id},
            )
            raise RuntimeError('kill before commit')

    workflow.refresh_from_db()
    assert workflow.status == WebSearchWorkflow.Status.APPLY_PENDING
    assert not run.evidence.exists()
    with transaction.atomic():
        WebResearchEvidence.objects.create(
            run=run,
            query='part',
            rank=1,
            url='https://example.com/part',
            domain='example.com',
        )
        acknowledge_web_search_workflow(
            workflow.pk,
            consumed_attempt_ids={first.attempt_id},
        )
    workflow.refresh_from_db()
    assert workflow.status == WebSearchWorkflow.Status.APPLIED


@pytest.mark.django_db
def test_workflow_input_and_logical_call_fingerprints_are_immutable():
    tenant = make_tenant('search-workflow-conflict')
    workflow = make_workflow(
        tenant,
        key='stable-workflow',
        domain='product:stable',
        snapshot={'queries': ['original'], 'providers': ['brave']},
    )
    first = execute_call(workflow, slot='query:0', query='original')
    second = execute_call(workflow, slot='query:1', query='second')

    assert first.attempt_id != second.attempt_id
    with pytest.raises(WebSearchWorkflowConflict):
        make_workflow(
            tenant,
            key='stable-workflow',
            domain='product:stable',
            snapshot={'queries': ['changed'], 'providers': ['tavily']},
        )
    with pytest.raises(WebSearchWorkflowConflict):
        execute_call(
            workflow,
            slot='query:0',
            query='changed',
            request_payload={
                'provider_id': 'brave', 'query': 'changed', 'slot': 'query:0',
            },
        )
    original_fingerprint = fingerprint_web_search_request({
        'provider_id': 'brave', 'query': 'original', 'slot': 'query:0',
    })
    with pytest.raises(WebSearchWorkflowConflict):
        reserve_web_search_attempt(
            workflow=workflow,
            provider_id='tavily',
            query='original',
            call_key=deterministic_web_search_call_key(
                provider_id='brave', call_kind='text', slot='query:0',
            ),
            request_fingerprint=original_fingerprint,
            call_kind='text',
        )
    with pytest.raises(ValueError, match='do not match'):
        acknowledge_web_search_workflow(
            workflow.pk,
            consumed_attempt_ids={first.attempt_id},
        )
    acknowledge_web_search_workflow(
        workflow.pk,
        consumed_attempt_ids={first.attempt_id, second.attempt_id},
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_logical_call_performs_at_most_one_outbound_request():
    tenant = make_tenant('search-concurrent-call')
    workflow = make_workflow(
        tenant, key='concurrent', domain='product:concurrent',
    )
    outbound = Mock(return_value=[{'ok': True}])

    def invoke():
        close_old_connections()
        try:
            local_workflow = WebSearchWorkflow.objects.get(pk=workflow.pk)
            try:
                execution = execute_call(local_workflow, call=outbound)
                return ('result', execution.attempt_id)
            except WebSearchProviderError as exc:
                return ('fenced', exc.code)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _value: invoke(), range(2)))

    assert outbound.call_count == 1
    assert WebSearchAttempt.objects.filter(workflow=workflow).count() == 1
    assert {outcome[0] for outcome in outcomes} <= {'result', 'fenced'}


@pytest.mark.django_db(transaction=True)
def test_executor_observing_started_call_invalidates_late_original_result():
    tenant = make_tenant('search-started-ownership-loss')
    workflow = make_workflow(
        tenant, key='ownership-loss', domain='product:ownership-loss',
    )
    sent = Event()
    release = Event()

    def delayed_provider():
        sent.set()
        assert release.wait(timeout=10)
        return [{'late': True}]

    def original_worker():
        close_old_connections()
        try:
            local = WebSearchWorkflow.objects.get(pk=workflow.pk)
            with pytest.raises(WebSearchProviderError) as discarded:
                execute_call(local, call=delayed_provider)
            return discarded.value.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(original_worker)
        assert sent.wait(timeout=10)
        with pytest.raises(WebSearchProviderError) as observer:
            execute_call(
                WebSearchWorkflow.objects.get(pk=workflow.pk),
                call=Mock(side_effect=AssertionError('must not send twice')),
            )
        assert observer.value.outcome_uncertain is True
        workflow.refresh_from_db()
        assert workflow.status == WebSearchWorkflow.Status.UNCERTAIN
        release.set()
        assert future.result(timeout=10) == 'provider_call_ownership_lost'

    attempt = workflow.attempts.get()
    assert attempt.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN
    assert attempt.checkpoint_enc is None
    assert attempt.reconciliation_state == (
        WebSearchAttempt.ReconciliationState.PENDING
    )


@pytest.mark.django_db
@pytest.mark.parametrize('failure_mode', ['oversized', 'codec', 'encryption'])
def test_checkpoint_failure_is_uncertain_and_never_returns_provider_result(
    settings, failure_mode,
):
    tenant = make_tenant(f'search-checkpoint-{failure_mode}')
    workflow = make_workflow(
        tenant, key=failure_mode, domain=f'product:{failure_mode}',
    )
    kwargs = {}
    context = patch('apps.web_research.accounting.encrypt')
    if failure_mode == 'oversized':
        settings.WEB_SEARCH_CHECKPOINT_MAX_BYTES = 32
        context = patch('apps.web_research.accounting.encrypt', wraps=__import__(
            'apps.datasources.encryption', fromlist=['encrypt'],
        ).encrypt)
    elif failure_mode == 'codec':
        kwargs['normalize_result'] = Mock(side_effect=TypeError('codec failed'))
    else:
        context = patch(
            'apps.web_research.accounting.encrypt',
            side_effect=RuntimeError('encryption failed'),
        )

    with context:
        with pytest.raises(WebSearchProviderError) as uncertain:
            execute_call(
                workflow,
                call=lambda: [{'payload': 'x' * 1000}],
                **kwargs,
            )

    assert uncertain.value.outcome_uncertain is True
    workflow.refresh_from_db()
    attempt = workflow.attempts.get()
    assert workflow.status == WebSearchWorkflow.Status.UNCERTAIN
    assert attempt.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN
    assert attempt.apply_state == WebSearchAttempt.ApplyState.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize('failure_mode', ['ciphertext', 'codec'])
def test_checkpoint_replay_failure_commits_uncertain_fence(failure_mode):
    tenant = make_tenant(f'search-replay-{failure_mode}')
    workflow = make_workflow(
        tenant,
        key=f'replay-{failure_mode}',
        domain=f'product:replay-{failure_mode}',
    )
    execution = execute_call(
        workflow,
        call=lambda: [{'durable': True}],
    )
    if failure_mode == 'ciphertext':
        WebSearchAttempt.objects.filter(pk=execution.attempt_id).update(
            checkpoint_enc=b'not-a-fernet-token',
        )

        def restore_result(value):
            return value
    else:
        restore_result = Mock(side_effect=TypeError('schema changed'))

    with pytest.raises(WebSearchProviderError) as uncertain:
        execute_call(
            workflow,
            call=Mock(side_effect=AssertionError('must replay, not send')),
            restore_result=restore_result,
        )

    assert uncertain.value.outcome_uncertain is True
    attempt = WebSearchAttempt.objects.get(pk=execution.attempt_id)
    workflow.refresh_from_db()
    assert attempt.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN
    assert attempt.reconciliation_state == (
        WebSearchAttempt.ReconciliationState.PENDING
    )
    assert workflow.status == WebSearchWorkflow.Status.UNCERTAIN
    with pytest.raises(WebSearchReconciliationRequired):
        make_workflow(
            tenant,
            key=f'new-after-broken-{failure_mode}',
            domain=workflow.domain_reference,
        )


@pytest.mark.django_db
def test_connection_and_global_limits_count_every_actual_provider_call(settings):
    settings.WEB_SEARCH_GLOBAL_REQUESTS_PER_MINUTE = 100
    settings.WEB_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT = 2
    settings.BRAVE_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT = 100
    settings.TAVILY_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT = 100
    tenant = make_tenant('search-global-limits')
    connection = WebSearchConnection.objects.create(
        provider_id='brave',
        display_name='Brave',
        requests_per_minute=100,
        monthly_request_limit=1,
    )
    first = make_workflow(tenant, key='one', domain='product:one')
    execute_call(first, connection=connection, call_kind='image')
    second = make_workflow(tenant, key='two', domain='product:two')
    with pytest.raises(WebSearchLimitExceeded):
        execute_call(second, connection=connection)

    third = make_workflow(tenant, key='three', domain='product:three')
    execute_call(third, provider_id='tavily')
    fourth = make_workflow(tenant, key='four', domain='product:four')
    with pytest.raises(WebSearchLimitExceeded):
        execute_call(fourth, provider_id='tavily')


@pytest.mark.django_db
def test_fresh_started_cannot_resolve_and_apply_pending_cannot_be_reconciled(settings):
    settings.WEB_SEARCH_STARTED_STALE_SECONDS = 900
    tenant = make_tenant('search-fresh-reconcile')
    workflow = make_workflow(tenant, key='fresh', domain='product:fresh')
    fingerprint = fingerprint_web_search_request({'query': 'part'})
    attempt, created = reserve_web_search_attempt(
        workflow=workflow,
        provider_id='brave',
        query='part',
        call_key='brave:text:slot',
        request_fingerprint=fingerprint,
    )
    assert created is True
    with pytest.raises(ValueError, match='still be in flight'):
        resolve_web_search_attempt(
            attempt.pk, action='not_accepted', operator_note='Too early.',
        )

    WebSearchAttempt.objects.filter(pk=attempt.pk).update(
        status=WebSearchAttempt.Status.SUCCESS,
        reconciliation_state=WebSearchAttempt.ReconciliationState.NOT_REQUIRED,
    )
    with pytest.raises(ValueError, match='does not require reconciliation'):
        resolve_web_search_attempt(
            attempt.pk, action='not_accepted', operator_note='Must apply checkpoint.',
        )


@pytest.mark.django_db(transaction=True)
def test_stale_manual_resolution_discards_late_provider_success(settings):
    settings.WEB_SEARCH_STARTED_STALE_SECONDS = 300
    tenant = make_tenant('search-late-result')
    workflow = make_workflow(tenant, key='late', domain='product:late')
    sent = Event()
    finish = Event()

    def provider_call():
        sent.set()
        assert finish.wait(timeout=10)
        return [{'late': True}]

    def worker():
        close_old_connections()
        try:
            local_workflow = WebSearchWorkflow.objects.get(pk=workflow.pk)
            with pytest.raises(WebSearchProviderError) as discarded:
                execute_call(local_workflow, call=provider_call)
            return discarded.value.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert sent.wait(timeout=10)
        attempt = WebSearchAttempt.objects.get(workflow=workflow)
        WebSearchAttempt.objects.filter(pk=attempt.pk).update(
            updated_at=now() - timedelta(seconds=301),
        )
        resolve_web_search_attempt(
            attempt.pk,
            action='not_accepted',
            operator_note='Provider console confirms no accepted request.',
        )
        finish.set()
        assert future.result(timeout=10) == 'provider_late_result_discarded'

    attempt.refresh_from_db()
    workflow.refresh_from_db()
    assert attempt.status == WebSearchAttempt.Status.STARTED
    assert attempt.reconciliation_state == WebSearchAttempt.ReconciliationState.RESOLVED
    assert workflow.status == WebSearchWorkflow.Status.RECONCILED


@pytest.mark.django_db(transaction=True)
def test_stale_manual_resolution_discards_late_safe_provider_failure(settings):
    settings.WEB_SEARCH_STARTED_STALE_SECONDS = 300
    tenant = make_tenant('search-late-safe-failure')
    workflow = make_workflow(
        tenant, key='late-safe-failure', domain='product:late-safe-failure',
    )
    sent = Event()
    finish = Event()

    def provider_call():
        sent.set()
        assert finish.wait(timeout=10)
        raise WebSearchProviderError(
            'Documented late rejection.',
            code='http_400',
        )

    def worker():
        close_old_connections()
        try:
            local_workflow = WebSearchWorkflow.objects.get(pk=workflow.pk)
            with pytest.raises(WebSearchProviderError) as discarded:
                execute_call(local_workflow, call=provider_call)
            return discarded.value.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert sent.wait(timeout=10)
        attempt = WebSearchAttempt.objects.get(workflow=workflow)
        WebSearchAttempt.objects.filter(pk=attempt.pk).update(
            updated_at=now() - timedelta(seconds=301),
        )
        resolve_web_search_attempt(
            attempt.pk,
            action='not_accepted',
            operator_note='Provider console confirms no accepted request.',
        )
        finish.set()
        assert future.result(timeout=10) == 'provider_late_result_discarded'

    attempt.refresh_from_db()
    workflow.refresh_from_db()
    assert attempt.status == WebSearchAttempt.Status.STARTED
    assert attempt.reconciliation_state == (
        WebSearchAttempt.ReconciliationState.RESOLVED
    )
    assert workflow.status == WebSearchWorkflow.Status.RECONCILED


@pytest.mark.django_db
def test_active_workflow_protects_product_run_attempt_and_tenant_deletion():
    tenant = make_tenant('search-owner-protection')
    product = make_product(tenant)
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    workflow = make_workflow(
        tenant,
        key='owner',
        domain=f'product:{product.pk}:purpose:enrichment',
        product=product,
        run=run,
    )
    execution = execute_call(workflow)

    with pytest.raises(ProtectedError), transaction.atomic():
        run.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        WebResearchRun.objects.filter(pk=run.pk).delete()
    attempt = WebSearchAttempt.objects.get(pk=execution.attempt_id)
    assert attempt.run_id == run.pk
    with pytest.raises(ProtectedError), transaction.atomic():
        attempt.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        workflow.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        product.hard_delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        tenant.delete()

    acknowledge_web_search_workflow(
        workflow.pk,
        consumed_attempt_ids={attempt.pk},
    )
    # A provider checkpoint can be acknowledged into evidence before the
    # remaining local pricing/AI phase finishes. Its canonical run remains a
    # durable owner until that phase reaches a terminal outcome.
    with pytest.raises(ProtectedError), transaction.atomic():
        run.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        product.hard_delete()
    attempt.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        workflow.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        WebSearchWorkflow.objects.filter(pk=workflow.pk).delete()
    run.status = WebResearchRun.Status.COMPLETED
    run.finished_at = now()
    run.save(update_fields=['status', 'finished_at', 'updated_at'])
    workflow.delete()
    run.delete()
    product.hard_delete()


@pytest.mark.django_db
def test_reconciliation_command_exact_confirmation_releases_domain_idempotently():
    tenant = make_tenant('search-reconcile-command')
    workflow = make_workflow(tenant, key='command', domain='product:command')
    fingerprint = fingerprint_web_search_request({'query': 'part'})
    attempt, _ = reserve_web_search_attempt(
        workflow=workflow,
        provider_id='brave',
        query='part',
        call_key='brave:text:slot',
        request_fingerprint=fingerprint,
    )
    WebSearchAttempt.objects.filter(pk=attempt.pk).update(
        status=WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
    )
    with pytest.raises(CommandError, match='exactly match'):
        call_command(
            'reconcile_web_search_outcome',
            attempt_id=attempt.pk,
            confirm='wrong',
            action='not_accepted',
            note='Checked provider console.',
        )

    output = StringIO()
    call_command(
        'reconcile_web_search_outcome',
        attempt_id=attempt.pk,
        confirm=str(attempt.pk),
        action='not_accepted',
        note='Checked provider console.',
        stdout=output,
    )
    call_command(
        'reconcile_web_search_outcome',
        attempt_id=attempt.pk,
        confirm=str(attempt.pk),
        action='not_accepted',
        note='Repeat is safe.',
        stdout=StringIO(),
    )
    with pytest.raises(CommandError, match='different action'):
        call_command(
            'reconcile_web_search_outcome',
            attempt_id=attempt.pk,
            confirm=str(attempt.pk),
            action='accepted',
            note='Conflicting decision.',
        )
    assert str(attempt.pk) in output.getvalue()
    assert 'Checked provider console' not in output.getvalue()

    next_workflow = make_workflow(
        tenant, key='next', domain='product:command',
    )
    assert next_workflow.pk != workflow.pk
