import uuid
from datetime import datetime, timedelta
from decimal import Decimal
import json
import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai_agent.models import AIProviderOperation
from apps.ai_agent.protection import unresolved_ai_provider_operation_q
from apps.billing.ai_wallet import AIReservation, AIWalletService
from apps.tenants.models import Tenant


logger = logging.getLogger(__name__)
MAX_VALIDATED_RESULT_BYTES = 256 * 1024


class AIProviderOperationStateError(RuntimeError):
    """The requested accounting transition is unsafe for the current state."""


class AIProviderReconciliationRequired(AIProviderOperationStateError):
    """A paid call for this domain is blocked by an unresolved prior outcome."""


@transaction.atomic
def begin_ai_provider_operation(
    *,
    tenant: Tenant,
    task_type: str,
    provider: str,
    model_id: str,
    reserved_amount: Decimal,
    domain_type: str,
    domain_reference: str,
    reservation_details: dict[str, Any] | None = None,
) -> AIProviderOperation:
    """Atomically reserve credits and persist the pre-network-call audit row."""
    normalized_reference = str(domain_reference).strip()
    if not normalized_reference:
        raise ValueError('domain_reference is required')
    if domain_type not in AIProviderOperation.DomainType.values:
        raise ValueError('unsupported domain_type')

    # Serialize all paid starts for a tenant, then fail closed for this exact
    # business domain. A second intent must never cross the provider boundary
    # while an older reservation/outcome still requires reconciliation.
    Tenant.objects.select_for_update().only('pk').get(pk=tenant.pk)
    # Lock and validate the durable domain owner after the tenant lock. Delete
    # guards use the same Tenant -> owner -> operation order, so a concurrent
    # hard delete can neither orphan a newly-created string-reference row nor
    # deadlock with a provider start.
    if domain_type == AIProviderOperation.DomainType.PRODUCT:
        from apps.products.models import Product
        owner_exists = Product.all_objects.select_for_update().filter(
            pk=normalized_reference,
            tenant_id=tenant.pk,
        ).exists()
    else:
        from apps.web_research.models import WebResearchRun
        owner_exists = WebResearchRun.objects.select_for_update().filter(
            pk=normalized_reference,
            tenant_id=tenant.pk,
        ).exists()
    if not owner_exists:
        raise ValueError('AI provider operation domain owner does not exist')
    if AIProviderOperation.objects.filter(
        tenant=tenant,
        task_type=task_type,
        domain_type=domain_type,
        domain_reference=normalized_reference,
    ).filter(unresolved_ai_provider_operation_q()).exists():
        raise AIProviderReconciliationRequired(
            'provider_reconciliation_required',
        )

    reservation = AIWalletService.reserve(
        tenant,
        reserved_amount,
        details=reservation_details,
    )
    return AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=task_type,
        provider=provider,
        model_id=model_id,
        reservation_key=reservation.key,
        reserved_amount=reservation.amount,
        domain_type=domain_type,
        domain_reference=normalized_reference,
    )


@transaction.atomic
def mark_ai_provider_network_started(
    operation_id: uuid.UUID,
) -> AIProviderOperation:
    """Persist the paid-provider boundary immediately before ``call_model``."""
    operation = AIProviderOperation.objects.select_for_update().get(pk=operation_id)
    if operation.status != AIProviderOperation.Status.RESERVED:
        raise AIProviderOperationStateError(
            f'Cannot start provider call for operation in status {operation.status}.',
        )
    if operation.network_started_at is None:
        operation.network_started_at = timezone.now()
        operation.save(update_fields=['network_started_at', 'updated_at'])
    return operation


@transaction.atomic
def mark_ai_provider_operation_uncertain(
    operation_id: uuid.UUID,
    *,
    error_code: str,
) -> AIProviderOperation:
    """Keep the reservation held and expose the operation for manual review."""
    operation = AIProviderOperation.objects.select_for_update().get(pk=operation_id)
    if operation.status in {
        AIProviderOperation.Status.RELEASED,
        AIProviderOperation.Status.SETTLED,
    }:
        return operation
    if operation.status == AIProviderOperation.Status.PENDING_RECONCILIATION:
        return operation
    if operation.status != AIProviderOperation.Status.RESERVED:
        raise AIProviderOperationStateError(
            f'Unsupported operation status {operation.status}.',
        )

    changed_fields = ['status', 'provider_error_code', 'updated_at']
    operation.status = AIProviderOperation.Status.PENDING_RECONCILIATION
    operation.provider_error_code = error_code[:80]
    if operation.uncertainty_marked_at is None:
        operation.uncertainty_marked_at = timezone.now()
        changed_fields.append('uncertainty_marked_at')
    operation.save(update_fields=changed_fields)
    return operation


@transaction.atomic
def release_ai_provider_operation(
    operation_id: uuid.UUID,
    *,
    reason: str,
) -> AIProviderOperation:
    operation = AIProviderOperation.objects.select_for_update().get(pk=operation_id)
    if operation.status == AIProviderOperation.Status.RELEASED:
        return operation
    if operation.status == AIProviderOperation.Status.SETTLED:
        raise AIProviderOperationStateError('A settled operation cannot be released.')

    _release_locked(operation, reason=reason)
    return operation


@transaction.atomic
def settle_ai_provider_operation(
    operation_id: uuid.UUID,
    *,
    actual_amount: Decimal,
    details: dict[str, Any] | None = None,
    terminal_reason: str = 'provider_response_received',
    validated_result: dict[str, Any] | None = None,
    apply_required: bool = False,
) -> tuple[AIProviderOperation, Decimal]:
    operation = AIProviderOperation.objects.select_for_update().get(pk=operation_id)
    if operation.status == AIProviderOperation.Status.SETTLED:
        return operation, operation.charged_amount or Decimal('0')
    if operation.status == AIProviderOperation.Status.RELEASED:
        raise AIProviderOperationStateError('A released operation cannot be settled.')

    charged = _settle_locked(
        operation,
        actual_amount=actual_amount,
        details=details,
        reason=terminal_reason,
        resolution_action=AIProviderOperation.ResolutionAction.SETTLE,
        validated_result=validated_result,
        apply_required=apply_required,
    )
    return operation, charged


@transaction.atomic
def resolve_uncertain_ai_provider_operation(
    operation_id: uuid.UUID,
    *,
    action: str,
    operator_note: str,
) -> tuple[AIProviderOperation, bool]:
    """Resolve exactly one uncertain operation under a row lock.

    Repeating the same decision is a no-op.  A conflicting decision is rejected
    so an operator cannot reverse an already committed wallet transaction.
    """
    note = operator_note.strip()
    if not note:
        raise ValueError('operator_note is required')
    if action not in {
        AIProviderOperation.ResolutionAction.RELEASE,
        AIProviderOperation.ResolutionAction.SETTLE_RESERVED,
    }:
        raise ValueError('unsupported reconciliation action')

    operation = AIProviderOperation.objects.select_for_update().get(pk=operation_id)
    target_status = (
        AIProviderOperation.Status.RELEASED
        if action == AIProviderOperation.ResolutionAction.RELEASE
        else AIProviderOperation.Status.SETTLED
    )
    if operation.status == target_status:
        return operation, False
    if operation.status in {
        AIProviderOperation.Status.RELEASED,
        AIProviderOperation.Status.SETTLED,
    }:
        raise AIProviderOperationStateError(
            f'Operation is already terminal with status {operation.status}.',
        )
    if operation.status != AIProviderOperation.Status.PENDING_RECONCILIATION:
        raise AIProviderOperationStateError(
            'Only an operation pending reconciliation can be resolved manually.',
        )

    if action == AIProviderOperation.ResolutionAction.RELEASE:
        _release_locked(
            operation,
            reason='manual_reconciliation',
            operator_note=note,
        )
    else:
        _settle_locked(
            operation,
            actual_amount=operation.reserved_amount,
            details={'reason': 'manual_reconciliation'},
            reason='manual_reconciliation',
            operator_note=note,
            resolution_action=AIProviderOperation.ResolutionAction.SETTLE_RESERVED,
        )
    return operation, True


@transaction.atomic
def apply_description_provider_operation(
    operation_id: uuid.UUID,
) -> dict[str, Any]:
    """Idempotently apply one exact paid description result to its product."""
    operation = (
        AIProviderOperation.objects.select_for_update()
        .select_related('tenant')
        .get(pk=operation_id)
    )
    if (
        operation.status != AIProviderOperation.Status.SETTLED
        or operation.domain_type != AIProviderOperation.DomainType.PRODUCT
        or operation.task_type != 'description_generation'
    ):
        raise AIProviderOperationStateError(
            'Operation is not a settled description result.',
        )
    if operation.apply_state == AIProviderOperation.ApplyState.APPLIED:
        return _description_result(operation.validated_result)
    if operation.apply_state != AIProviderOperation.ApplyState.PENDING:
        raise AIProviderOperationStateError(
            'Description operation is not pending domain application.',
        )

    result = _description_result(operation.validated_result)
    try:
        product_id = int(operation.domain_reference)
    except (TypeError, ValueError) as exc:
        raise AIProviderOperationStateError(
            'Description operation has an invalid product reference.',
        ) from exc

    from apps.marketplaces.models import Listing
    from apps.products.models import Product

    try:
        product = Product.objects.select_for_update().get(
            pk=product_id,
            tenant_id=operation.tenant_id,
        )
    except Product.DoesNotExist as exc:
        raise AIProviderOperationStateError(
            'Description result product no longer exists.',
        ) from exc

    Product.objects.filter(pk=product.pk).update(
        title_ai=result['title'],
        description_ai=result['description'],
    )
    Listing.objects.filter(
        product_id=product.pk,
        status__in=[
            Listing.STATUS_DRAFT,
            Listing.STATUS_REQUIRES_REVIEW,
            Listing.STATUS_REJECTED,
        ],
    ).update(
        title=result['title'],
        description_ai=result['description'],
        ai_confidence=result['confidence'],
    )
    applied_at = timezone.now()
    operation.apply_state = AIProviderOperation.ApplyState.APPLIED
    operation.applied_at = applied_at
    operation.save(update_fields=['apply_state', 'applied_at', 'updated_at'])
    return result


def pending_description_operation_id(
    *,
    tenant_id: int,
    product_id: int,
) -> uuid.UUID | None:
    return (
        AIProviderOperation.objects.filter(
            tenant_id=tenant_id,
            task_type='description_generation',
            domain_type=AIProviderOperation.DomainType.PRODUCT,
            domain_reference=str(product_id),
            status=AIProviderOperation.Status.SETTLED,
            apply_state=AIProviderOperation.ApplyState.PENDING,
        )
        .order_by('created_at')
        .values_list('pk', flat=True)
        .first()
    )


def apply_pending_ai_provider_results(*, limit: int = 200) -> dict[str, int]:
    """Best-effort recovery of paid results after a worker hard crash."""
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be between 1 and 1000')
    operation_ids = list(
        AIProviderOperation.objects.filter(
            status=AIProviderOperation.Status.SETTLED,
            apply_state=AIProviderOperation.ApplyState.PENDING,
        )
        .order_by('created_at')
        .values_list('pk', flat=True)[:limit]
    )
    applied = 0
    failed = 0
    for operation_id in operation_ids:
        try:
            operation = AIProviderOperation.objects.only('domain_type').get(
                pk=operation_id,
            )
            if operation.domain_type == AIProviderOperation.DomainType.PRODUCT:
                apply_description_provider_operation(operation_id)
            elif (
                operation.domain_type
                == AIProviderOperation.DomainType.WEB_RESEARCH_RUN
            ):
                from apps.web_research.services import WebResearchService
                WebResearchService.apply_ai_provider_operation(operation_id)
            else:
                raise AIProviderOperationStateError(
                    f'Unsupported result domain {operation.domain_type}.',
                )
        except Exception:
            failed += 1
            logger.exception(
                'Failed to apply durable AI provider result: operation=%s',
                operation_id,
            )
        else:
            applied += 1
    return {'selected': len(operation_ids), 'applied': applied, 'failed': failed}


def reconcile_stale_ai_provider_operations(
    *,
    current_time: datetime | None = None,
    started_timeout_seconds: int | None = None,
    never_started_timeout_seconds: int | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Recover reservations orphaned by a hard process/host failure.

    A persisted network marker means provider acceptance is possible, so the
    reservation is held for an operator. An old row without the marker proves
    that this application never crossed the provider boundary and is safe to
    release automatically.
    """
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be between 1 and 1000')
    started_timeout = int(
        started_timeout_seconds
        if started_timeout_seconds is not None
        else getattr(settings, 'AI_PROVIDER_STARTED_STALE_SECONDS', 600)
    )
    never_started_timeout = int(
        never_started_timeout_seconds
        if never_started_timeout_seconds is not None
        else getattr(settings, 'AI_PROVIDER_NEVER_STARTED_STALE_SECONDS', 300)
    )
    if started_timeout < 1 or never_started_timeout < 1:
        raise ValueError('stale timeouts must be positive')

    checked_at = current_time or timezone.now()
    pending_count = 0
    released_count = 0
    with transaction.atomic():
        stale_started = list(
            AIProviderOperation.objects.select_for_update(skip_locked=True)
            .filter(
                status=AIProviderOperation.Status.RESERVED,
                network_started_at__isnull=False,
                network_started_at__lte=(
                    checked_at - timedelta(seconds=started_timeout)
                ),
            )
            .select_related('tenant')
            .order_by('network_started_at', 'created_at')[:limit]
        )
        for operation in stale_started:
            operation.status = AIProviderOperation.Status.PENDING_RECONCILIATION
            operation.provider_error_code = 'stale_provider_call'
            operation.uncertainty_marked_at = checked_at
            operation.save(update_fields=[
                'status', 'provider_error_code', 'uncertainty_marked_at',
                'updated_at',
            ])
            pending_count += 1

        remaining = limit - pending_count
        if remaining:
            stale_never_started = list(
                AIProviderOperation.objects.select_for_update(skip_locked=True)
                .filter(
                    status=AIProviderOperation.Status.RESERVED,
                    network_started_at__isnull=True,
                    created_at__lte=(
                        checked_at - timedelta(seconds=never_started_timeout)
                    ),
                )
                .select_related('tenant')
                .order_by('created_at')[:remaining]
            )
            for operation in stale_never_started:
                _release_locked(
                    operation,
                    reason='provider_call_never_started',
                )
                released_count += 1

    return {
        'pending_reconciliation': pending_count,
        'released_never_started': released_count,
    }


def _reservation(operation: AIProviderOperation) -> AIReservation:
    return AIReservation(
        key=operation.reservation_key,
        amount=operation.reserved_amount,
    )


def _release_locked(
    operation: AIProviderOperation,
    *,
    reason: str,
    operator_note: str = '',
) -> None:
    AIWalletService.release(
        operation.tenant,
        _reservation(operation),
        reason=reason,
    )
    resolved_at = timezone.now()
    operation.status = AIProviderOperation.Status.RELEASED
    operation.charged_amount = Decimal('0')
    operation.terminal_reason = reason[:120]
    operation.resolution_action = AIProviderOperation.ResolutionAction.RELEASE
    operation.operator_note = operator_note
    operation.validated_result = None
    operation.apply_state = AIProviderOperation.ApplyState.NOT_REQUIRED
    operation.released_at = resolved_at
    operation.resolved_at = resolved_at
    operation.save(update_fields=[
        'status', 'charged_amount', 'terminal_reason', 'resolution_action',
        'operator_note', 'validated_result', 'apply_state',
        'released_at', 'resolved_at', 'updated_at',
    ])


def _settle_locked(
    operation: AIProviderOperation,
    *,
    actual_amount: Decimal,
    details: dict[str, Any] | None,
    reason: str,
    operator_note: str = '',
    resolution_action: str,
    validated_result: dict[str, Any] | None = None,
    apply_required: bool = False,
) -> Decimal:
    if apply_required and validated_result is None:
        raise ValueError('validated_result is required for domain application')
    normalized_result = (
        _bounded_validated_result(validated_result)
        if validated_result is not None else None
    )
    charged = AIWalletService.settle(
        operation.tenant,
        _reservation(operation),
        actual_amount,
        details=details,
    )
    resolved_at = timezone.now()
    operation.status = AIProviderOperation.Status.SETTLED
    operation.charged_amount = charged
    operation.terminal_reason = reason[:120]
    operation.resolution_action = resolution_action
    operation.operator_note = operator_note
    operation.validated_result = normalized_result
    operation.apply_state = (
        AIProviderOperation.ApplyState.PENDING
        if apply_required
        else AIProviderOperation.ApplyState.NOT_REQUIRED
    )
    operation.settled_at = resolved_at
    operation.resolved_at = resolved_at
    operation.save(update_fields=[
        'status', 'charged_amount', 'terminal_reason', 'resolution_action',
        'operator_note', 'validated_result', 'apply_state',
        'settled_at', 'resolved_at', 'updated_at',
    ])
    return charged


def _bounded_validated_result(
    result: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError('validated_result must be an object')
    try:
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError('validated_result must contain bounded JSON values') from exc
    if len(serialized.encode('utf-8')) > MAX_VALIDATED_RESULT_BYTES:
        raise ValueError('validated_result exceeds the 256 KiB limit')
    normalized = json.loads(serialized)
    if not isinstance(normalized, dict):
        raise ValueError('validated_result must be an object')
    return normalized


def _description_result(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AIProviderOperationStateError('Description result payload is missing.')
    title = value.get('title')
    description = value.get('description')
    confidence = value.get('confidence')
    if (
        not isinstance(title, str)
        or not 1 <= len(title) <= 200
        or not isinstance(description, str)
        or not 1 <= len(description) <= 7500
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise AIProviderOperationStateError(
            'Stored description result failed domain validation.',
        )
    return dict(value)
