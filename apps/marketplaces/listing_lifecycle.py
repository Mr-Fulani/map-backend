"""Provider-neutral persistence helpers for remote listing observations.

``Listing.status`` remains the canonical local workflow state.  This module
only builds updates for the remote observation, its next due cursor, and the
short-lived claim used by a future batch scheduler.

There are deliberately two observation paths:

* :func:`record_remote_observation` is safe for transitional dual writes and
  does not touch a claim owned by another worker;
* :func:`complete_claimed_status_check` clears the claim; its caller must use
  a provider-specific queryset containing the complete intent/account/live
  fence before persisting it.

The scheduler and backfill remain outside this module.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.utils import timezone

from apps.marketplaces.models import Listing


CANONICAL_REMOTE_STATUSES = frozenset(
    value for value, _label in Listing.REMOTE_STATUS_CHOICES
)

_LIFECYCLE_FIELDS = frozenset({
    'remote_status',
    'remote_status_checked_at',
    'next_status_check_at',
    'status_check_claim_token',
    'status_check_claimed_until',
})


@dataclass(frozen=True, slots=True)
class ListingLifecycleUpdate:
    """An immutable, validated set of fields for one lifecycle write.

    ``update_fields`` can be passed directly to ``Model.save`` after
    ``apply_to``.  ``as_update_kwargs`` can be expanded into
    ``QuerySet.update`` without allowing the canonical local ``status`` field
    into this helper's write set.
    """

    _items: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        field_names = tuple(name for name, _value in self._items)
        duplicates = {name for name in field_names if field_names.count(name) > 1}
        if duplicates:
            raise ValueError(f'Duplicate lifecycle fields: {sorted(duplicates)!r}')
        unsupported = set(field_names) - _LIFECYCLE_FIELDS
        if unsupported:
            raise ValueError(f'Unsupported lifecycle fields: {sorted(unsupported)!r}')

    @property
    def update_fields(self) -> tuple[str, ...]:
        return tuple(name for name, _value in self._items)

    def as_update_kwargs(self) -> dict[str, object]:
        return dict(self._items)

    def apply_to(self, listing: Listing) -> tuple[str, ...]:
        for field_name, value in self._items:
            setattr(listing, field_name, value)
        return self.update_fields


def _update(**values: object) -> ListingLifecycleUpdate:
    return ListingLifecycleUpdate(tuple(values.items()))


def _normalized_status_key(value: object) -> str:
    if not isinstance(value, str):
        return ''
    return value.strip().casefold()


def _validated_aliases(aliases: Mapping[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for alias, target in (aliases or {}).items():
        alias_key = _normalized_status_key(alias)
        target_key = _normalized_status_key(target)
        if not alias_key:
            raise ValueError('Remote status aliases must have non-empty string keys.')
        if target_key not in CANONICAL_REMOTE_STATUSES:
            raise ValueError(f'Alias target is not a canonical remote status: {target!r}')
        previous = normalized.setdefault(alias_key, target_key)
        if previous != target_key:
            raise ValueError(f'Conflicting aliases after normalization: {alias!r}')
    return normalized


def normalize_remote_status(
    raw_status: object,
    *,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Map a provider value into the bounded remote-status vocabulary.

    Canonical values are case-insensitive.  Provider-specific semantics must
    be supplied explicitly through ``aliases``; unknown, empty, and malformed
    values become ``other`` instead of leaking provider vocabulary into the
    shared schema.
    """

    status_key = _normalized_status_key(raw_status)
    status_key = _validated_aliases(aliases).get(status_key, status_key)
    if status_key in CANONICAL_REMOTE_STATUSES:
        return status_key
    return Listing.REMOTE_STATUS_OTHER


def _aware_datetime(value: datetime | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError(f'{field_name} must be a timezone-aware datetime or None.')
    return value


def _claim_token(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError('claim_token must be a non-null UUID.') from None


def schedule_status_check(next_status_check_at: datetime | None) -> ListingLifecycleUpdate:
    """Set or clear the due cursor without changing observation or claim state."""

    due_at = _aware_datetime(next_status_check_at, field_name='next_status_check_at')
    return _update(next_status_check_at=due_at)


def claim_status_check(
    *,
    claim_token: UUID | str,
    claimed_until: datetime,
) -> ListingLifecycleUpdate:
    """Build claim values for an atomic scheduler-side queryset update."""

    token = _claim_token(claim_token)
    lease_until = _aware_datetime(claimed_until, field_name='claimed_until')
    if lease_until is None:
        raise ValueError('claimed_until must not be None.')
    return _update(
        status_check_claim_token=token,
        status_check_claimed_until=lease_until,
    )


def release_status_check(
    *,
    next_status_check_at: datetime | None,
) -> ListingLifecycleUpdate:
    """Clear a claim and optionally reschedule it after a non-observation."""

    due_at = _aware_datetime(next_status_check_at, field_name='next_status_check_at')
    return _update(
        next_status_check_at=due_at,
        status_check_claim_token=None,
        status_check_claimed_until=None,
    )


def record_remote_observation(
    raw_status: object,
    *,
    checked_at: datetime,
    next_status_check_at: datetime | None,
    aliases: Mapping[str, str] | None = None,
) -> ListingLifecycleUpdate:
    """Build a dual-write-safe observation update without touching a claim."""

    observed_at = _aware_datetime(checked_at, field_name='checked_at')
    if observed_at is None:
        raise ValueError('checked_at must not be None.')
    due_at = _aware_datetime(next_status_check_at, field_name='next_status_check_at')
    return _update(
        remote_status=normalize_remote_status(raw_status, aliases=aliases),
        remote_status_checked_at=observed_at,
        next_status_check_at=due_at,
    )


def complete_claimed_status_check(
    raw_status: object,
    *,
    checked_at: datetime,
    next_status_check_at: datetime | None,
    aliases: Mapping[str, str] | None = None,
) -> ListingLifecycleUpdate:
    """Build a successful observation update that also releases its claim."""

    observation = record_remote_observation(
        raw_status,
        checked_at=checked_at,
        next_status_check_at=next_status_check_at,
        aliases=aliases,
    )
    return _update(
        **observation.as_update_kwargs(),
        status_check_claim_token=None,
        status_check_claimed_until=None,
    )


def clear_remote_observation() -> ListingLifecycleUpdate:
    """Forget remote evidence while preserving schedule, claim, and local status."""

    return _update(
        remote_status=None,
        remote_status_checked_at=None,
    )
