"""Shared configuration for image-search adapters managed in the admin panel."""

from dataclasses import dataclass, field

from django.conf import settings
from django.db import DatabaseError


@dataclass(frozen=True)
class ImageSourceConnection:
    enabled: bool
    priority: int
    credentials: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)


def _tenant_plan_slug(tenant) -> str:
    try:
        subscription = tenant.subscription
        return subscription.plan.slug if subscription.is_active else ''
    except Exception:
        return ''


def image_source_connection(provider_id: str, tenant=None) -> ImageSourceConnection:
    """Resolve admin-managed provider settings while retaining env compatibility."""
    try:
        from apps.web_research.models import WebSearchConnection

        connection = WebSearchConnection.objects.filter(provider_id=provider_id).first()
    except DatabaseError:
        connection = None

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
    )


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
