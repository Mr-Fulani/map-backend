"""Fail-closed admission for Ozon account and mutation canaries."""

from django.conf import settings


OZON_PRODUCT_IMPORT_METHOD = '/v3/product/import'
OZON_PRODUCT_ARCHIVE_METHOD = '/v1/product/archive'


def _safe_string_allowlist(setting_name: str, *, max_length: int) -> frozenset[str]:
    values = getattr(settings, setting_name, ())
    if not isinstance(values, (tuple, list, frozenset, set)):
        return frozenset()
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        for value in values
    ):
        return frozenset()
    return frozenset(values)


def ozon_connection_enabled_for_tenant(tenant) -> bool:
    """Expose O1c only to an explicitly allowlisted tenant."""

    tenant_slug = str(getattr(tenant, 'slug', '') or '')
    tenant_slugs = _safe_string_allowlist(
        'OZON_ACCOUNT_CONNECTION_TENANT_SLUGS',
        max_length=50,
    )
    return (
        getattr(settings, 'OZON_ACCOUNT_CONNECTION_ENABLED', False) is True
        and tenant_slug in tenant_slugs
    )


def ozon_connection_enabled_for_account(tenant, client_id: object) -> bool:
    """Admit one exact tenant/client pair before any provider request."""

    normalized_client_id = str(client_id or '').strip()
    client_ids = _safe_string_allowlist(
        'OZON_ACCOUNT_CONNECTION_CLIENT_IDS',
        max_length=100,
    )
    return (
        ozon_connection_enabled_for_tenant(tenant)
        and normalized_client_id in client_ids
    )


def ozon_product_write_enabled_for_account(account) -> bool:
    """Require both the existing exact-account gate and a DB kill switch."""

    from apps.marketplaces.models import OzonAccountProfile

    if not ozon_connection_enabled_for_account(
        getattr(account, 'tenant', None),
        getattr(account, 'external_id', ''),
    ):
        return False
    try:
        profile = account.ozon_profile
    except (AttributeError, OzonAccountProfile.DoesNotExist):
        return False
    return (
        profile.product_write_enabled is True
        and OZON_PRODUCT_IMPORT_METHOD in profile.api_methods
    )


def ozon_product_archive_enabled_for_account(account) -> bool:
    """Require the product gate plus the exact archive endpoint permission."""

    if not ozon_product_write_enabled_for_account(account):
        return False
    return OZON_PRODUCT_ARCHIVE_METHOD in account.ozon_profile.api_methods
