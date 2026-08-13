"""Central durability predicate for paid AI provider operations."""

from django.db.models import Q


def unresolved_ai_provider_operation_q() -> Q:
    """Rows that still own held money, an unknown outcome, or paid payload."""
    from apps.ai_agent.models import AIProviderOperation

    return (
        Q(status__in=[
            AIProviderOperation.Status.RESERVED,
            AIProviderOperation.Status.PENDING_RECONCILIATION,
        ])
        | Q(
            status=AIProviderOperation.Status.SETTLED,
            apply_state=AIProviderOperation.ApplyState.PENDING,
        )
    )


def is_unresolved_ai_provider_operation(operation) -> bool:
    from apps.ai_agent.models import AIProviderOperation

    return (
        operation.status in {
            AIProviderOperation.Status.RESERVED,
            AIProviderOperation.Status.PENDING_RECONCILIATION,
        }
        or (
            operation.status == AIProviderOperation.Status.SETTLED
            and operation.apply_state == AIProviderOperation.ApplyState.PENDING
        )
    )
