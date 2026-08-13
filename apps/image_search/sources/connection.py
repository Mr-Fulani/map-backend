"""Shared configuration for image-search adapters managed in the admin panel."""

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist


@dataclass(frozen=True)
class ImageSourceConnection:
    enabled: bool
    priority: int
    credentials: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    database_connection: Any | None = None


def _tenant_plan_slug(tenant) -> str:
    if tenant is None:
        return ''
    try:
        subscription = tenant.subscription
        return subscription.plan.slug if subscription.is_active else ''
    except ObjectDoesNotExist:
        return ''


def image_source_connection(provider_id: str, tenant=None) -> ImageSourceConnection:
    """Resolve managed settings; database uncertainty must fail closed.

    Environment credentials remain a compatibility fallback only when a
    successful database query proves that no managed row exists. Treating a
    database failure as a missing row could otherwise send a paid request with
    stale/global credentials while an admin-disabled connection is unreadable.
    """
    from apps.web_research.models import WebSearchConnection

    connection = WebSearchConnection.objects.filter(provider_id=provider_id).first()

    if connection is None:
        return ImageSourceConnection(enabled=True, priority=100)
    if not connection.is_active:
        return ImageSourceConnection(enabled=False, priority=connection.priority)

    plan_slug = _tenant_plan_slug(tenant)
    if connection.allowed_plan_slugs and plan_slug not in connection.allowed_plan_slugs:
        return ImageSourceConnection(enabled=False, priority=connection.priority)

    return ImageSourceConnection(
        enabled=True,
        priority=connection.priority,
        credentials=connection.get_credentials(),
        parameters=connection.parameters or {},
        database_connection=connection,
    )


def image_search_domain_reference(product) -> str:
    """Stable cross-intent domain used to fence unresolved paid calls."""
    tenant_id = getattr(product, 'tenant_id', None)
    product_id = getattr(product, 'pk', None)
    if not tenant_id or not product_id:
        raise ValueError('persisted tenant-owned product is required')
    return f'product:{tenant_id}:{product_id}'


def execute_recorded_image_search(
    source,
    *,
    query: str,
    slot: str,
    call,
    request_payload: object,
    result_count=len,
):
    """Replay or run one immutable paid image-provider logical slot."""
    from apps.web_research.accounting import (
        WebSearchExecution,
        deterministic_web_search_call_key,
        execute_recorded_web_search,
        fingerprint_web_search_request,
        replay_recorded_web_search,
    )

    product = source.product
    tenant = getattr(product, 'tenant', None)
    if tenant is None:
        raise ValueError('persisted tenant-owned product is required')
    workflow = getattr(source, 'web_search_workflow', None)
    if workflow is None:
        raise ValueError('durable web-search workflow is required')
    request_fingerprint = fingerprint_web_search_request(request_payload)
    call_key = deterministic_web_search_call_key(
        provider_id=source.provider_id,
        call_kind='image',
        slot=slot,
    )

    # This lookup deliberately precedes credentials, connection state and
    # provider availability. A paid checkpoint remains usable after an admin
    # disables the provider or rotates/removes its key.
    try:
        replay: WebSearchExecution[Any] | None = replay_recorded_web_search(
            workflow,
            call_key=call_key,
            request_fingerprint=request_fingerprint,
        )
    except Exception as exc:
        source.consume_attempt(getattr(exc, 'attempt_id', None))
        raise
    if replay is not None:
        source.consume_attempt(replay.attempt_id)
        return replay.result

    connection = image_source_connection(source.source_id, tenant)
    if not connection.enabled:
        from apps.web_research.providers.base import WebSearchProviderError
        raise WebSearchProviderError(
            f'{source.source_id} image search connection is disabled.',
            retryable=False,
            code='provider_disabled',
        )
    workflow_plan = getattr(source, 'workflow_plan', None)
    if not isinstance(workflow_plan, dict) or 'connection_id' not in workflow_plan:
        from apps.web_research.providers.base import WebSearchProviderError
        raise WebSearchProviderError(
            'Paid image-search workflow connection identity is missing.',
            retryable=False,
            code='provider_request_conflict',
            outcome_uncertain=True,
        )
    planned_connection_id = workflow_plan.get('connection_id')
    current_connection_id = getattr(connection.database_connection, 'pk', None)
    if planned_connection_id != current_connection_id:
        from apps.web_research.providers.base import WebSearchProviderError
        raise WebSearchProviderError(
            'Snapshotted image-search connection is no longer available.',
            retryable=False,
            code='provider_connection_changed',
        )
    try:
        execution = execute_recorded_web_search(
            workflow=workflow,
            provider=source,
            connection=connection.database_connection,
            query=query,
            call_key=call_key,
            request_fingerprint=request_fingerprint,
            call=call,
            call_kind='image',
            result_count=result_count,
        )
    except Exception as exc:
        source.consume_attempt(getattr(exc, 'attempt_id', None))
        raise
    # Pure adapter tests may replace the wrapper with a provider-native value.
    attempt_id = getattr(execution, 'attempt_id', None)
    source.consume_attempt(attempt_id)
    return getattr(execution, 'result', execution)


def image_source_api_key(
    provider_id: str, tenant, setting_name: str, fallback_value: str = '',
) -> str:
    connection = image_source_connection(provider_id, tenant)
    if not connection.enabled:
        return ''
    return str(
        connection.credentials.get('api_key')
        or fallback_value
        or getattr(settings, setting_name, '')
        or ''
    )
