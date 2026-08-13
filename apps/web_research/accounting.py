"""Durable workflow, quota reservation, and checkpoints for paid web search."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import timedelta
import hashlib
import json
import time
from typing import Any, Generic, Sized, TypeVar, cast

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.timezone import now

from apps.datasources.encryption import decrypt, encrypt
from apps.tenants.models import Tenant
from apps.web_research.models import (
    WebResearchRun,
    WebSearchAttempt,
    WebSearchConnection,
    WebSearchUsageGate,
    WebSearchWorkflow,
)
from apps.web_research.providers.base import WebSearchProviderError


ResultT = TypeVar('ResultT')


class WebSearchLimitExceeded(WebSearchProviderError):
    """The call was rejected before transmission by an atomic quota gate."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(
            f'{provider_id} search limit is exhausted.',
            retryable=False,
            code='provider_limit_exhausted',
        )


class WebSearchReconciliationRequired(WebSearchProviderError):
    """Another workflow for the same business domain is still active."""

    def __init__(self) -> None:
        super().__init__(
            'A prior provider request requires reconciliation or domain apply.',
            retryable=False,
            code='provider_reconciliation_required',
            outcome_uncertain=True,
        )


class WebSearchWorkflowConflict(WebSearchProviderError):
    """A stable workflow or logical call key was reused with changed input."""

    def __init__(self) -> None:
        super().__init__(
            'The durable web-search workflow conflicts with its original input.',
            retryable=False,
            code='provider_request_conflict',
            outcome_uncertain=True,
        )


@dataclass(frozen=True)
class WebSearchExecution(Generic[ResultT]):
    result: ResultT
    attempt_id: int
    workflow_id: int
    replayed: bool


def _identity_result(value: object) -> Any:
    return value


def _result_length(value: ResultT) -> int:
    return len(cast(Sized, value))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')


def fingerprint_web_search_request(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def deterministic_web_search_call_key(
    *, provider_id: str, call_kind: str, slot: str,
) -> str:
    """Return a logical call identity independent from mutable request bytes."""
    normalized = ':'.join([
        str(provider_id).strip().lower(),
        str(call_kind).strip().lower() or 'search',
        str(slot).strip(),
    ])
    if not normalized.rsplit(':', 1)[-1]:
        raise ValueError('slot is required')
    if len(normalized) <= 160:
        return normalized
    digest = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    return f'{normalized[:95]}:{digest}'


def _validate_snapshot(snapshot: object) -> tuple[object, str]:
    encoded = _canonical_json(snapshot)
    limit = settings.WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES
    if len(encoded) > limit:
        raise ValueError('web-search workflow input exceeds the configured limit')
    # Round-trip through JSON so callers always drive execution from the exact
    # persisted normalized shape, not from mutable runtime Python objects.
    normalized = json.loads(encoded.decode('utf-8'))
    return normalized, hashlib.sha256(encoded).hexdigest()


@transaction.atomic
def acquire_web_search_workflow(
    *,
    tenant,
    operation: str,
    domain_reference: str,
    workflow_key: str,
    input_snapshot: object,
    product=None,
    run: WebResearchRun | None = None,
) -> WebSearchWorkflow:
    """Acquire or replay one immutable durable business execution."""
    normalized_operation = str(operation).strip().lower()[:50]
    normalized_reference = str(domain_reference).strip()[:160]
    normalized_key = str(workflow_key).strip()[:160]
    if not normalized_operation or not normalized_reference or not normalized_key:
        raise ValueError('operation, domain_reference, and workflow_key are required')
    if run is not None and run.tenant_id != tenant.pk:
        raise ValueError('run does not belong to tenant')
    if product is not None and product.tenant_id != tenant.pk:
        raise ValueError('product does not belong to tenant')
    normalized_snapshot, input_fingerprint = _validate_snapshot(input_snapshot)

    type(tenant).objects.select_for_update().only('pk').get(pk=tenant.pk)
    canonical = WebSearchWorkflow.objects.select_for_update().filter(
        tenant=tenant,
        operation=normalized_operation,
        workflow_key=normalized_key,
    ).first()
    if canonical is not None:
        if (
            canonical.domain_reference != normalized_reference
            or canonical.input_fingerprint != input_fingerprint
            or canonical.input_snapshot != normalized_snapshot
            or (run is not None and canonical.run_id not in {None, run.pk})
            or (product is not None and canonical.product_id not in {None, product.pk})
        ):
            raise WebSearchWorkflowConflict()
        update_fields = []
        if canonical.run_id is None and run is not None:
            canonical.run = run
            update_fields.append('run')
        if (
            canonical.product_id is None
            and product is not None
            and canonical.status in WebSearchWorkflow.ACTIVE_STATUSES
        ):
            canonical.product = product
            update_fields.append('product')
        if update_fields:
            update_fields.append('updated_at')
            canonical.save(update_fields=update_fields)
        return canonical

    active = WebSearchWorkflow.objects.filter(
        tenant=tenant,
        operation=normalized_operation,
        domain_reference=normalized_reference,
        status__in=WebSearchWorkflow.ACTIVE_STATUSES,
    )
    if active.exists():
        raise WebSearchReconciliationRequired()
    try:
        return WebSearchWorkflow.objects.create(
            tenant=tenant,
            product=product,
            run=run,
            operation=normalized_operation,
            domain_reference=normalized_reference,
            workflow_key=normalized_key,
            input_fingerprint=input_fingerprint,
            input_snapshot=normalized_snapshot,
        )
    except IntegrityError as exc:
        # The tenant lock makes the normal race deterministic; preserve a
        # stable domain error if an external writer bypasses that lock.
        raise WebSearchReconciliationRequired() from exc


@transaction.atomic
def resume_web_search_workflow(
    *, tenant, operation: str, workflow_key: str,
) -> WebSearchWorkflow:
    """Load a canonical workflow without recomputing its immutable plan."""
    normalized_operation = str(operation).strip().lower()[:50]
    normalized_key = str(workflow_key).strip()[:160]
    type(tenant).objects.select_for_update().only('pk').get(pk=tenant.pk)
    return WebSearchWorkflow.objects.select_for_update().get(
        tenant=tenant,
        operation=normalized_operation,
        workflow_key=normalized_key,
    )


def _counted_attempts():
    return WebSearchAttempt.objects.exclude(
        Q(status=WebSearchAttempt.Status.SKIPPED)
        | Q(
            status=WebSearchAttempt.Status.FAILED,
            error_code='pre_send_failure',
        )
    )


def _provider_monthly_limit(provider_id: str) -> int:
    if provider_id == 'brave':
        return settings.BRAVE_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT
    if provider_id == 'tavily':
        return settings.TAVILY_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT
    return settings.WEB_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT


def _durable_provider_error(attempt: WebSearchAttempt) -> WebSearchProviderError:
    uncertain = (
        attempt.reconciliation_state
        == WebSearchAttempt.ReconciliationState.PENDING
    )
    error = WebSearchProviderError(
        attempt.error_message or 'Recorded provider request did not return a usable result.',
        retryable=attempt.retryable,
        code=attempt.error_code or 'provider_error',
        outcome_uncertain=uncertain,
    )
    error.attempt_id = attempt.pk
    error.workflow_id = attempt.workflow_id
    return error


def _decode_checkpoint(attempt: WebSearchAttempt) -> object:
    if attempt.checkpoint_enc is None:
        raise WebSearchProviderError(
            'Paid provider result checkpoint is unavailable.',
            code='provider_checkpoint_missing',
            outcome_uncertain=True,
        )
    try:
        payload = decrypt(bytes(attempt.checkpoint_enc))
    except Exception as exc:
        raise WebSearchProviderError(
            'Paid provider result checkpoint cannot be decrypted.',
            code='provider_checkpoint_invalid',
            outcome_uncertain=True,
        ) from exc
    if not isinstance(payload, dict) or payload.get('version') != 1 or 'result' not in payload:
        raise WebSearchProviderError(
            'Paid provider result checkpoint is invalid.',
            code='provider_checkpoint_invalid',
            outcome_uncertain=True,
        )
    return payload['result']


@transaction.atomic
def _mark_checkpoint_replay_uncertain(
    attempt_id: int,
    *,
    error_code: str,
) -> WebSearchAttempt:
    """Fence a paid result whose durable checkpoint cannot be restored."""
    tenant_id, workflow_id = WebSearchAttempt.objects.values_list(
        'tenant_id', 'workflow_id',
    ).get(pk=attempt_id)
    Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
    workflow = WebSearchWorkflow.objects.select_for_update().get(pk=workflow_id)
    attempt = WebSearchAttempt.objects.select_for_update().get(pk=attempt_id)
    if (
        attempt.status in {
            WebSearchAttempt.Status.SUCCESS,
            WebSearchAttempt.Status.EMPTY,
        }
        and attempt.apply_state == WebSearchAttempt.ApplyState.PENDING
        and attempt.reconciliation_state
        == WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        and workflow.status in WebSearchWorkflow.ACTIVE_STATUSES
    ):
        attempt.status = WebSearchAttempt.Status.OUTCOME_UNCERTAIN
        attempt.retryable = False
        attempt.error_code = str(error_code)[:80]
        attempt.error_message = (
            'Paid provider checkpoint could not be restored safely.'
        )
        attempt.reconciliation_state = (
            WebSearchAttempt.ReconciliationState.PENDING
        )
        attempt.save(update_fields=[
            'status', 'retryable', 'error_code', 'error_message',
            'reconciliation_state', 'updated_at',
        ])
        workflow.status = WebSearchWorkflow.Status.UNCERTAIN
        workflow.save(update_fields=['status', 'updated_at'])
    return attempt


def _restore_execution(
    attempt: WebSearchAttempt,
    *,
    request_fingerprint: str,
    restore_result: Callable[[object], ResultT],
) -> WebSearchExecution[ResultT]:
    if attempt.request_fingerprint != request_fingerprint:
        raise WebSearchWorkflowConflict()
    if attempt.status in {
        WebSearchAttempt.Status.SUCCESS,
        WebSearchAttempt.Status.EMPTY,
    }:
        try:
            result = restore_result(_decode_checkpoint(attempt))
        except WebSearchProviderError as exc:
            _mark_checkpoint_replay_uncertain(
                attempt.pk,
                error_code=exc.code or 'provider_checkpoint_invalid',
            )
            exc.attempt_id = attempt.pk
            exc.workflow_id = attempt.workflow_id
            raise
        except Exception as exc:
            _mark_checkpoint_replay_uncertain(
                attempt.pk,
                error_code='provider_checkpoint_codec_error',
            )
            error = WebSearchProviderError(
                'Paid provider checkpoint codec failed.',
                code='provider_checkpoint_codec_error',
                outcome_uncertain=True,
            )
            error.attempt_id = attempt.pk
            error.workflow_id = attempt.workflow_id
            raise error from exc
        return WebSearchExecution(
            result=result,
            attempt_id=attempt.pk,
            workflow_id=attempt.workflow_id,
            replayed=True,
        )
    raise _durable_provider_error(attempt)


def replay_recorded_web_search(
    workflow: WebSearchWorkflow,
    *,
    call_key: str,
    request_fingerprint: str,
    restore_result: Callable[[object], ResultT] = _identity_result,
) -> WebSearchExecution[ResultT] | None:
    """Replay one logical slot before resolving credentials/provider routing."""
    deferred_error: WebSearchProviderError | None = None
    execution: WebSearchExecution[ResultT] | None = None
    with transaction.atomic():
        Tenant.objects.select_for_update().only('pk').get(pk=workflow.tenant_id)
        locked = WebSearchWorkflow.objects.select_for_update().get(pk=workflow.pk)
        attempt = locked.attempts.select_for_update().filter(call_key=call_key).first()
        if attempt is None:
            return None
        if (
            attempt.status == WebSearchAttempt.Status.STARTED
            and attempt.reconciliation_state
            == WebSearchAttempt.ReconciliationState.PENDING
            and locked.status != WebSearchWorkflow.Status.UNCERTAIN
        ):
            # A second executor observed a call whose original ownership may
            # have been lost. Poison the workflow before returning the fence
            # so a late original worker cannot cross into domain apply after
            # lease recovery. Catch the durable error inside this transaction
            # so the poison commits before it is surfaced to the caller.
            locked.status = WebSearchWorkflow.Status.UNCERTAIN
            locked.save(update_fields=['status', 'updated_at'])
        try:
            execution = _restore_execution(
                attempt,
                request_fingerprint=request_fingerprint,
                restore_result=restore_result,
            )
        except WebSearchProviderError as exc:
            deferred_error = exc
    if deferred_error is not None:
        raise deferred_error
    return execution


@transaction.atomic
def reserve_web_search_attempt(
    *,
    workflow: WebSearchWorkflow,
    provider_id: str,
    query: str,
    call_key: str,
    request_fingerprint: str,
    call_kind: str = 'search',
    connection: WebSearchConnection | None = None,
    run: WebResearchRun | None = None,
) -> tuple[WebSearchAttempt, bool]:
    """Reserve quota and persist STARTED immediately before network I/O."""
    normalized_provider = str(provider_id).strip().lower()
    normalized_call_key = str(call_key).strip()[:160]
    if not normalized_provider or not normalized_call_key:
        raise ValueError('provider_id and call_key are required')
    if len(request_fingerprint) != 64:
        raise ValueError('request_fingerprint must be a SHA-256 digest')

    tenant = workflow.tenant
    type(tenant).objects.select_for_update().only('pk').get(pk=tenant.pk)
    locked_workflow = WebSearchWorkflow.objects.select_for_update().get(pk=workflow.pk)
    expected_run = run if run is not None else locked_workflow.run
    expected_run_id = expected_run.pk if expected_run is not None else None
    existing = locked_workflow.attempts.select_for_update().filter(
        call_key=normalized_call_key,
    ).first()
    if existing is not None:
        if (
            existing.request_fingerprint != request_fingerprint
            or existing.provider_id != normalized_provider
            or existing.call_kind
            != (str(call_kind).strip().lower()[:30] or 'search')
            or existing.query != str(query).strip()[:500]
            or existing.connection_id
            != (connection.pk if connection is not None else None)
            or existing.run_id != expected_run_id
        ):
            raise WebSearchWorkflowConflict()
        if (
            existing.status == WebSearchAttempt.Status.STARTED
            and existing.reconciliation_state
            == WebSearchAttempt.ReconciliationState.PENDING
            and locked_workflow.status != WebSearchWorkflow.Status.UNCERTAIN
        ):
            locked_workflow.status = WebSearchWorkflow.Status.UNCERTAIN
            locked_workflow.save(update_fields=['status', 'updated_at'])
        return existing, False
    if locked_workflow.status not in {
        WebSearchWorkflow.Status.IN_PROGRESS,
        WebSearchWorkflow.Status.APPLY_PENDING,
    }:
        raise WebSearchReconciliationRequired()
    if run is not None and run.tenant_id != tenant.pk:
        raise ValueError('run does not belong to tenant')

    WebSearchUsageGate.objects.get_or_create(provider_id='global')
    WebSearchUsageGate.objects.select_for_update().get(provider_id='global')
    WebSearchUsageGate.objects.get_or_create(provider_id=normalized_provider)
    if normalized_provider != 'global':
        WebSearchUsageGate.objects.select_for_update().get(
            provider_id=normalized_provider,
        )

    current = now()
    all_counted = _counted_attempts()
    counted = all_counted.filter(provider_id=normalized_provider)
    global_minute_limit = settings.WEB_SEARCH_GLOBAL_REQUESTS_PER_MINUTE
    if global_minute_limit and all_counted.filter(
        created_at__gte=current - timedelta(minutes=1),
    ).count() >= global_minute_limit:
        raise WebSearchLimitExceeded(normalized_provider)
    platform_monthly_limit = settings.WEB_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT
    if platform_monthly_limit and all_counted.filter(
        created_at__year=current.year,
        created_at__month=current.month,
    ).count() >= platform_monthly_limit:
        raise WebSearchLimitExceeded(normalized_provider)
    provider_monthly_limit = _provider_monthly_limit(normalized_provider)
    if provider_monthly_limit and counted.filter(
        created_at__year=current.year,
        created_at__month=current.month,
    ).count() >= provider_monthly_limit:
        raise WebSearchLimitExceeded(normalized_provider)

    locked_connection = None
    if connection is not None:
        locked_connection = WebSearchConnection.objects.select_for_update().get(
            pk=connection.pk,
        )
        if not locked_connection.is_active:
            raise WebSearchLimitExceeded(normalized_provider)
        connection_attempts = counted.filter(connection=locked_connection)
        if locked_connection.requests_per_minute and connection_attempts.filter(
            created_at__gte=current - timedelta(minutes=1),
        ).count() >= locked_connection.requests_per_minute:
            raise WebSearchLimitExceeded(normalized_provider)
        if locked_connection.monthly_request_limit and connection_attempts.filter(
            created_at__year=current.year,
            created_at__month=current.month,
        ).count() >= locked_connection.monthly_request_limit:
            raise WebSearchLimitExceeded(normalized_provider)

    try:
        # Keep a uniqueness race inside a savepoint so the outer quota/domain
        # transaction remains usable for canonical-row recovery.
        with transaction.atomic():
            attempt = WebSearchAttempt.objects.create(
                tenant=tenant,
                workflow=locked_workflow,
                run=run or locked_workflow.run,
                connection=locked_connection,
                provider_id=normalized_provider,
                operation=locked_workflow.operation,
                call_kind=str(call_kind).strip().lower()[:30] or 'search',
                domain_reference=locked_workflow.domain_reference,
                call_key=normalized_call_key,
                request_fingerprint=request_fingerprint,
                query=str(query).strip()[:500],
                status=WebSearchAttempt.Status.STARTED,
                apply_state=WebSearchAttempt.ApplyState.PENDING,
                reconciliation_state=WebSearchAttempt.ReconciliationState.PENDING,
            )
        return attempt, True
    except IntegrityError:
        existing = locked_workflow.attempts.select_for_update().get(
            call_key=normalized_call_key,
        )
        if (
            existing.request_fingerprint != request_fingerprint
            or existing.provider_id != normalized_provider
            or existing.call_kind
            != (str(call_kind).strip().lower()[:30] or 'search')
            or existing.query != str(query).strip()[:500]
            or existing.connection_id
            != (connection.pk if connection is not None else None)
        ):
            raise WebSearchWorkflowConflict()
        return existing, False


@transaction.atomic
def finalize_web_search_attempt(
    attempt_id: int,
    *,
    status: str,
    result_count: int = 0,
    duration_ms: int = 0,
    retryable: bool = False,
    error_code: str = '',
    error_message: str = '',
    normalized_result: object | None = None,
) -> WebSearchAttempt:
    tenant_id, workflow_id = WebSearchAttempt.objects.values_list(
        'tenant_id', 'workflow_id',
    ).get(pk=attempt_id)
    Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
    workflow = WebSearchWorkflow.objects.select_for_update().get(
        pk=workflow_id,
    )
    attempt = (
        WebSearchAttempt.objects.select_for_update()
        .select_related('workflow')
        .get(pk=attempt_id)
    )
    if (
        attempt.status != WebSearchAttempt.Status.STARTED
        or attempt.reconciliation_state == WebSearchAttempt.ReconciliationState.RESOLVED
    ):
        return attempt
    if workflow.status == WebSearchWorkflow.Status.UNCERTAIN:
        # Another executor already observed this in-flight row and invalidated
        # the original caller's ownership. Persist no late result and never
        # allow that stale caller to mutate domain state after recovery.
        attempt.status = WebSearchAttempt.Status.OUTCOME_UNCERTAIN
        attempt.result_count = 0
        attempt.duration_ms = max(0, int(duration_ms))
        attempt.retryable = False
        attempt.error_code = 'provider_call_ownership_lost'
        attempt.error_message = (
            'Provider response arrived after execution ownership was lost.'
        )
        attempt.checkpoint_enc = None
        attempt.reconciliation_state = (
            WebSearchAttempt.ReconciliationState.PENDING
        )
        attempt.save(update_fields=[
            'status', 'result_count', 'duration_ms', 'retryable',
            'error_code', 'error_message', 'checkpoint_enc',
            'reconciliation_state', 'updated_at',
        ])
        return attempt
    attempt.status = status
    attempt.result_count = max(0, min(int(result_count), 65535))
    attempt.duration_ms = max(0, int(duration_ms))
    attempt.retryable = bool(retryable)
    attempt.error_code = str(error_code)[:80]
    attempt.error_message = str(error_message)[:500]
    success = status in {
        WebSearchAttempt.Status.SUCCESS,
        WebSearchAttempt.Status.EMPTY,
    }
    uncertain = status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN
    if success:
        try:
            payload = {'version': 1, 'result': normalized_result}
            plaintext = _canonical_json(payload)
            if len(plaintext) > settings.WEB_SEARCH_CHECKPOINT_MAX_BYTES:
                raise ValueError('checkpoint exceeds configured limit')
            attempt.checkpoint_enc = encrypt(json.loads(plaintext.decode('utf-8')))
            attempt.reconciliation_state = (
                WebSearchAttempt.ReconciliationState.NOT_REQUIRED
            )
            workflow.status = WebSearchWorkflow.Status.APPLY_PENDING
        except Exception:
            attempt.status = WebSearchAttempt.Status.OUTCOME_UNCERTAIN
            attempt.error_code = 'provider_checkpoint_failed'
            attempt.error_message = 'Provider result checkpoint could not be persisted.'
            attempt.reconciliation_state = (
                WebSearchAttempt.ReconciliationState.PENDING
            )
            workflow.status = WebSearchWorkflow.Status.UNCERTAIN
    elif uncertain:
        workflow.status = WebSearchWorkflow.Status.UNCERTAIN
    else:
        # A documented rejection/pre-send failure is safe to fall through to
        # another logical slot in this same workflow. It still remains pending
        # domain acknowledgement so a changed runtime plan cannot erase it.
        attempt.reconciliation_state = WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        workflow.status = (
            WebSearchWorkflow.Status.APPLY_PENDING
            if workflow.attempts.exclude(pk=attempt.pk).filter(
                apply_state=WebSearchAttempt.ApplyState.PENDING,
                status__in=[
                    WebSearchAttempt.Status.SUCCESS,
                    WebSearchAttempt.Status.EMPTY,
                ],
            ).exists()
            else WebSearchWorkflow.Status.IN_PROGRESS
        )
    attempt.save(update_fields=[
        'status', 'result_count', 'duration_ms', 'retryable',
        'error_code', 'error_message', 'checkpoint_enc',
        'reconciliation_state', 'updated_at',
    ])
    workflow.save(update_fields=['status', 'updated_at'])
    return attempt


@transaction.atomic
def acknowledge_web_search_workflow(
    workflow_id: int,
    *,
    consumed_attempt_ids: set[int] | list[int] | tuple[int, ...],
) -> WebSearchWorkflow:
    """Atomically mark exactly the caller-consumed provider evidence applied."""
    tenant_id = WebSearchWorkflow.objects.values_list(
        'tenant_id', flat=True,
    ).get(pk=workflow_id)
    Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
    workflow = WebSearchWorkflow.objects.select_for_update().get(pk=workflow_id)
    supplied = {int(value) for value in consumed_attempt_ids}
    attempts = list(workflow.attempts.select_for_update().order_by('pk'))
    expected = {attempt.pk for attempt in attempts}
    if not attempts:
        raise ValueError('empty workflow requires explicit no-network release')
    if supplied != expected:
        raise ValueError('consumed attempt ids do not match the persisted workflow plan')
    if workflow.status == WebSearchWorkflow.Status.APPLIED:
        return workflow
    if workflow.status not in {
        WebSearchWorkflow.Status.IN_PROGRESS,
        WebSearchWorkflow.Status.APPLY_PENDING,
    }:
        raise ValueError('workflow cannot be acknowledged in its current state')
    if any(
        attempt.reconciliation_state == WebSearchAttempt.ReconciliationState.PENDING
        for attempt in attempts
    ):
        raise ValueError('workflow contains an unresolved provider call')
    WebSearchAttempt.objects.filter(pk__in=expected).update(
        apply_state=WebSearchAttempt.ApplyState.APPLIED,
        updated_at=now(),
    )
    workflow.status = WebSearchWorkflow.Status.APPLIED
    workflow.applied_at = now()
    workflow.product = None
    workflow.save(update_fields=['status', 'applied_at', 'product', 'updated_at'])
    return workflow


@transaction.atomic
def release_empty_web_search_workflow(workflow_id: int) -> WebSearchWorkflow:
    """Close a workflow only when no provider reservation was ever created."""
    tenant_id = WebSearchWorkflow.objects.values_list(
        'tenant_id', flat=True,
    ).get(pk=workflow_id)
    Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
    workflow = WebSearchWorkflow.objects.select_for_update().get(pk=workflow_id)
    if workflow.attempts.select_for_update().exists():
        raise ValueError('workflow has provider attempts and cannot be released empty')
    if workflow.status == WebSearchWorkflow.Status.APPLIED:
        return workflow
    if workflow.status != WebSearchWorkflow.Status.IN_PROGRESS:
        raise ValueError('workflow cannot be released in its current state')
    workflow.status = WebSearchWorkflow.Status.APPLIED
    workflow.applied_at = now()
    workflow.product = None
    workflow.save(update_fields=['status', 'applied_at', 'product', 'updated_at'])
    return workflow


@transaction.atomic
def resolve_web_search_attempt(
    attempt_id: int,
    *,
    action: str,
    operator_note: str,
) -> WebSearchAttempt:
    """Explicitly close one genuinely unknown provider outcome."""
    if action not in {'accepted', 'not_accepted'}:
        raise ValueError('unsupported reconciliation action')
    note = str(operator_note).strip()
    if not note:
        raise ValueError('operator_note is required')
    tenant_id, workflow_id = WebSearchAttempt.objects.values_list(
        'tenant_id', 'workflow_id',
    ).get(pk=attempt_id)
    Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
    workflow = WebSearchWorkflow.objects.select_for_update().get(
        pk=workflow_id,
    )
    attempt = WebSearchAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.reconciliation_state == WebSearchAttempt.ReconciliationState.RESOLVED:
        if attempt.reconciliation_action != action:
            raise ValueError('attempt was resolved with a different action')
        return attempt
    if attempt.reconciliation_state != WebSearchAttempt.ReconciliationState.PENDING:
        raise ValueError('attempt does not require reconciliation')
    if attempt.status == WebSearchAttempt.Status.STARTED:
        stale_before = now() - timedelta(
            seconds=settings.WEB_SEARCH_STARTED_STALE_SECONDS,
        )
        if attempt.updated_at > stale_before:
            raise ValueError('started attempt may still be in flight')
    elif attempt.status != WebSearchAttempt.Status.OUTCOME_UNCERTAIN:
        # In particular, a successful APPLY_PENDING checkpoint cannot be
        # released by an operator without the caller applying its exact value.
        raise ValueError('only uncertain attempts can be reconciled')
    attempt.reconciliation_state = WebSearchAttempt.ReconciliationState.RESOLVED
    attempt.reconciliation_action = action
    attempt.reconciliation_note = note
    attempt.reconciled_at = now()
    attempt.apply_state = WebSearchAttempt.ApplyState.APPLIED
    attempt.save(update_fields=[
        'reconciliation_state', 'reconciliation_action',
        'reconciliation_note', 'reconciled_at', 'apply_state', 'updated_at',
    ])
    # Earlier slots that ended in a documented rejection/pre-send failure are
    # already authoritative audit evidence; they need no separate operator
    # decision when this workflow's sole unknown slot is reconciled.
    workflow.attempts.filter(
        reconciliation_state=WebSearchAttempt.ReconciliationState.NOT_REQUIRED,
        status__in=[
            WebSearchAttempt.Status.FAILED,
            WebSearchAttempt.Status.SKIPPED,
        ],
    ).update(
        apply_state=WebSearchAttempt.ApplyState.APPLIED,
        updated_at=now(),
    )
    unresolved = workflow.attempts.exclude(
        reconciliation_state=WebSearchAttempt.ReconciliationState.RESOLVED,
    ).filter(
        Q(status__in=[
            WebSearchAttempt.Status.STARTED,
            WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
        ])
        | Q(reconciliation_state=WebSearchAttempt.ReconciliationState.PENDING)
        | Q(apply_state=WebSearchAttempt.ApplyState.PENDING)
    )
    if not unresolved.exists():
        workflow.status = WebSearchWorkflow.Status.RECONCILED
        workflow.reconciliation_action = action
        workflow.reconciliation_note = note
        workflow.reconciled_at = now()
        workflow.product = None
        workflow.save(update_fields=[
            'status', 'reconciliation_action', 'reconciliation_note',
            'reconciled_at', 'product', 'updated_at',
        ])
    else:
        has_unknown = workflow.attempts.filter(
            reconciliation_state=WebSearchAttempt.ReconciliationState.PENDING,
        ).exists()
        workflow.status = (
            WebSearchWorkflow.Status.UNCERTAIN
            if has_unknown
            else WebSearchWorkflow.Status.APPLY_PENDING
            if workflow.attempts.filter(
                apply_state=WebSearchAttempt.ApplyState.PENDING,
                status__in=[
                    WebSearchAttempt.Status.SUCCESS,
                    WebSearchAttempt.Status.EMPTY,
                ],
            ).exists()
            else WebSearchWorkflow.Status.IN_PROGRESS
        )
        workflow.save(update_fields=['status', 'updated_at'])
    return attempt


def _normalize_default(value: ResultT) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def execute_recorded_web_search(
    *,
    workflow: WebSearchWorkflow,
    provider,
    query: str,
    call_key: str,
    request_fingerprint: str,
    call: Callable[[], ResultT],
    call_kind: str = 'search',
    normalize_result: Callable[[ResultT], object] = _normalize_default,
    restore_result: Callable[[object], ResultT] = _identity_result,
    result_count: Callable[[ResultT], int] = _result_length,
    connection: WebSearchConnection | None = None,
    run: WebResearchRun | None = None,
) -> WebSearchExecution[ResultT]:
    """Execute or replay exactly one logical paid provider slot."""
    replay = replay_recorded_web_search(
        workflow,
        call_key=call_key,
        request_fingerprint=request_fingerprint,
        restore_result=restore_result,
    )
    if replay is not None:
        return replay
    attempt, created = reserve_web_search_attempt(
        workflow=workflow,
        provider_id=provider.provider_id,
        query=query,
        call_key=call_key,
        request_fingerprint=request_fingerprint,
        call_kind=call_kind,
        connection=connection,
        run=run,
    )
    if not created:
        return _restore_execution(
            attempt,
            request_fingerprint=request_fingerprint,
            restore_result=restore_result,
        )

    started = time.monotonic()
    try:
        result = call()
    except WebSearchProviderError as exc:
        finalized = finalize_web_search_attempt(
            attempt.pk,
            status=(
                WebSearchAttempt.Status.OUTCOME_UNCERTAIN
                if exc.outcome_uncertain else WebSearchAttempt.Status.FAILED
            ),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            retryable=exc.retryable,
            error_code=exc.code,
            error_message=str(exc),
        )
        if (
            finalized.status not in {
                WebSearchAttempt.Status.FAILED,
                WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
            }
            or finalized.reconciliation_state
            == WebSearchAttempt.ReconciliationState.RESOLVED
            or not WebSearchWorkflow.objects.filter(
                pk=finalized.workflow_id,
                status__in=WebSearchWorkflow.ACTIVE_STATUSES,
            ).exists()
        ):
            late_error = WebSearchProviderError(
                'Late provider outcome was discarded after reconciliation.',
                code='provider_late_result_discarded',
                outcome_uncertain=True,
            )
            late_error.attempt_id = finalized.pk
            late_error.workflow_id = finalized.workflow_id
            raise late_error from exc
        if finalized.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN:
            exc.outcome_uncertain = True
            exc.code = finalized.error_code or exc.code
        # A caller that deliberately consumes a documented safe failure must
        # include this exact evidence row in the workflow ACK set.
        exc.attempt_id = finalized.pk
        exc.workflow_id = finalized.workflow_id
        raise
    except Exception as exc:
        finalize_web_search_attempt(
            attempt.pk,
            status=WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code='unclassified_provider_error',
            error_message='Provider request outcome is uncertain.',
        )
        raise WebSearchProviderError(
            'Provider request outcome is uncertain; automatic retry is forbidden.',
            code='unclassified_provider_error',
            outcome_uncertain=True,
        ) from exc

    try:
        count = max(0, int(result_count(result)))
        normalized_result = normalize_result(result)
    except Exception as exc:
        finalize_web_search_attempt(
            attempt.pk,
            status=WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code='provider_checkpoint_codec_error',
            error_message='Provider result checkpoint codec failed.',
        )
        raise WebSearchProviderError(
            'Provider result checkpoint codec failed.',
            code='provider_checkpoint_codec_error',
            outcome_uncertain=True,
        ) from exc
    finalized = finalize_web_search_attempt(
        attempt.pk,
        status=(
            WebSearchAttempt.Status.SUCCESS
            if count else WebSearchAttempt.Status.EMPTY
        ),
        result_count=count,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        normalized_result=normalized_result,
    )
    if finalized.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN:
        raise WebSearchProviderError(
            'Provider result checkpoint could not be persisted.',
            code=finalized.error_code or 'provider_checkpoint_failed',
            outcome_uncertain=True,
        )
    if (
        finalized.status not in {
            WebSearchAttempt.Status.SUCCESS,
            WebSearchAttempt.Status.EMPTY,
        }
        or finalized.reconciliation_state
        != WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        or finalized.apply_state != WebSearchAttempt.ApplyState.PENDING
        or not WebSearchWorkflow.objects.filter(
            pk=finalized.workflow_id,
            status__in=WebSearchWorkflow.ACTIVE_STATUSES,
        ).exists()
    ):
        # An operator may have reconciled a stale STARTED row while the
        # original HTTP request was still completing. Never let that late
        # response cross the now-released domain fence.
        raise WebSearchProviderError(
            'Late provider result was discarded after reconciliation.',
            code='provider_late_result_discarded',
            outcome_uncertain=True,
        )
    return WebSearchExecution(
        result=result,
        attempt_id=attempt.pk,
        workflow_id=workflow.pk,
        replayed=False,
    )
