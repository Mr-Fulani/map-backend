from dataclasses import dataclass
from datetime import timedelta

from django.utils.timezone import now

from apps.web_research.models import WebSearchAttempt, WebSearchConnection
from apps.web_research.providers.base import BaseWebSearchProvider
from apps.web_research.providers.registry import registered_search_providers


@dataclass(frozen=True)
class SearchProviderCandidate:
    provider: BaseWebSearchProvider
    connection: WebSearchConnection | None = None


def _tenant_plan_slug(tenant) -> str:
    try:
        subscription = tenant.subscription
    except Exception:
        return ''
    return subscription.plan.slug if subscription.is_active else ''


def _within_limits(connection: WebSearchConnection) -> bool:
    attempts = WebSearchAttempt.objects.filter(connection=connection)
    if connection.requests_per_minute:
        recent = attempts.filter(created_at__gte=now() - timedelta(minutes=1)).count()
        if recent >= connection.requests_per_minute:
            return False
    if connection.monthly_request_limit:
        current = now()
        monthly = attempts.filter(
            created_at__year=current.year,
            created_at__month=current.month,
        ).count()
        if monthly >= connection.monthly_request_limit:
            return False
    return True


def search_provider_candidates(tenant, requested_provider: str = '') -> list[SearchProviderCandidate]:
    """Resolve configured providers by plan and limits, retaining env compatibility."""

    registry = registered_search_providers()
    plan_slug = _tenant_plan_slug(tenant)
    requested_provider = (requested_provider or '').strip().lower()
    connections = list(WebSearchConnection.objects.order_by('priority', 'display_name'))
    explicitly_managed = {connection.provider_id for connection in connections}
    result = []

    for connection in connections:
        if not connection.is_active or connection.provider_id not in registry:
            continue
        if requested_provider and connection.provider_id != requested_provider:
            continue
        if connection.allowed_plan_slugs and plan_slug not in connection.allowed_plan_slugs:
            continue
        if not _within_limits(connection):
            continue
        provider = registry[connection.provider_id](
            credentials=connection.get_credentials(),
            parameters=connection.parameters,
        )
        if provider.is_available():
            result.append(SearchProviderCandidate(provider, connection))

    # Existing production secrets remain valid until explicitly managed in the DB.
    for provider_id, provider_class in registry.items():
        if provider_id in explicitly_managed:
            continue
        if requested_provider and provider_id != requested_provider:
            continue
        provider = provider_class()
        if provider.is_available():
            result.append(SearchProviderCandidate(provider))
    return result
