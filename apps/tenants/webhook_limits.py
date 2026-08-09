"""Hard upper bounds for tenant webhook registration and dispatch."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


DEFAULT_WEBHOOK_ENDPOINTS_PER_TENANT = 20
MAX_WEBHOOK_ENDPOINTS_PER_TENANT = 100
DEFAULT_WEBHOOK_DISPATCH_BATCH_SIZE = 100
MAX_WEBHOOK_DISPATCH_BATCH_SIZE = 500


def _bounded_positive_setting(name: str, *, default: int, maximum: int) -> int:
    raw_value = getattr(settings, name, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f'{name} must be an integer.') from exc
    if not 1 <= value <= maximum:
        raise ImproperlyConfigured(
            f'{name} must be between 1 and {maximum}.',
        )
    return value


def webhook_endpoint_quota() -> int:
    """Maximum number of non-deleted endpoints owned by one tenant."""
    return _bounded_positive_setting(
        'WEBHOOK_ENDPOINTS_PER_TENANT',
        default=DEFAULT_WEBHOOK_ENDPOINTS_PER_TENANT,
        maximum=MAX_WEBHOOK_ENDPOINTS_PER_TENANT,
    )


def webhook_dispatch_batch_size() -> int:
    """Maximum delivery messages published by one dispatcher invocation."""
    return _bounded_positive_setting(
        'WEBHOOK_DISPATCH_BATCH_SIZE',
        default=DEFAULT_WEBHOOK_DISPATCH_BATCH_SIZE,
        maximum=MAX_WEBHOOK_DISPATCH_BATCH_SIZE,
    )
