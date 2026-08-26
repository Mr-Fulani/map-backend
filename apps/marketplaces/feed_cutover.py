"""Fail-closed admission for the account-scoped private feed cutover."""

from __future__ import annotations

from django.conf import settings


def private_feed_cutover_account_ids() -> frozenset[int]:
    """Return only validated positive integer IDs from Django settings."""

    values = getattr(settings, 'MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS', ())
    if not isinstance(values, (tuple, list, frozenset, set)):
        return frozenset()
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in values
    ):
        return frozenset()
    return frozenset(values)


def private_feed_fleet_enabled() -> bool:
    """Return whether every ready Avito account must use private delivery."""

    return (
        not private_feed_cutover_account_ids()
        and getattr(settings, 'AVITO_STATUS_LIFECYCLE_MODE', None) == 'dual_write'
        and getattr(settings, 'MARKETPLACE_FEED_RUN_MODE', None) == 'durable'
        and getattr(settings, 'MARKETPLACE_FEED_INGRESS_MODE', None) == 'dual_write'
        and getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', None) == 'active'
        and getattr(settings, 'MARKETPLACE_FEED_STORAGE_MODE', None) == 'stable_bridge'
        and getattr(settings, 'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED', None)
        is False
    )


def private_feed_cutover_enabled(account_id: object) -> bool:
    """Admit an exact canary account or every account in fleet mode."""

    if isinstance(account_id, bool) or not isinstance(account_id, int):
        return False
    account_ids = private_feed_cutover_account_ids()
    account_scoped = (
        account_id in account_ids
        and getattr(settings, 'MARKETPLACE_FEED_RUN_MODE', None) == 'legacy'
    )
    return (
        (account_scoped or private_feed_fleet_enabled())
        and getattr(settings, 'AVITO_STATUS_LIFECYCLE_MODE', None) == 'dual_write'
        and getattr(settings, 'MARKETPLACE_FEED_INGRESS_MODE', None) == 'dual_write'
        and getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', None) == 'active'
        and getattr(settings, 'MARKETPLACE_FEED_STORAGE_MODE', None) == 'stable_bridge'
        and getattr(settings, 'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED', None)
        is False
    )
