"""Resumable, account-scoped marketplace feed profile migration.

The workflow is intentionally narrow and fail closed:

``NEW -> MIGRATING -> BRIDGE_READY -> UPDATE_UNKNOWN -> VERIFIED``

``MIGRATING`` is a pre-POST bridge-probe checkpoint.  It is always safe to
repeat its provider GETs and direct/stable/direct parity probe, or to confirm
that its frozen source is still exact without promoting the checkpoint.
``UPDATE_UNKNOWN`` is the only durable provider-POST boundary.  It is committed
before exactly one physical Avito POST and can only resume through GET-only
reconciliation.

Complete profiles and feed URLs never leave this module.  Operator-facing
results contain only bounded counts, lifecycle state, revisions, and SHA-256
fingerprints.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import timedelta
import hmac
import math
import re
from collections.abc import Mapping

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.adapters.avito.error_handler import NotFoundError
from apps.marketplaces.adapters.avito.profile_migration import (
    AvitoProfileMigrationClient,
    AvitoProfileMigrationError,
    AvitoProfilePlan,
    AvitoProfilePostError,
    AvitoProfileTransportError,
    AvitoProfileValidationError,
    build_profile_plan,
    inspect_unprovisioned_profile,
    observe_endpoint_profile,
    probe_feed_bridge_parity,
    trusted_account_feed_object_key,
    validate_avito_profile_upsert_target,
)
from apps.marketplaces.feed_cutover import private_feed_fleet_enabled
from apps.marketplaces.feed_endpoint import (
    FeedEndpointConfigurationError,
    canonical_marketplace_feed_public_base_url,
    marketplace_feed_public_url,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint


_PHASES = frozenset({
    'inspect',
    'prepare',
    'migrate',
    'reconcile',
    'resolve-source',
    'confirm-prepare-source',
})
_FINGERPRINT_RE = re.compile(r'^[0-9a-f]{64}$')
_KEY_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')
_MAX_REVISION = (1 << 63) - 1


class FeedProfileMigrationError(RuntimeError):
    """Base class for redaction-safe operator workflow errors."""

    code = 'transport_failed'


class FeedProfileMigrationConflict(FeedProfileMigrationError):
    """The exact account, owner generation, state, or revision changed."""

    code = 'state_conflict'


class FeedProfileMigrationSafetyError(FeedProfileMigrationError):
    """Provider data or local configuration failed a safety invariant."""

    code = 'safety_refused'


class FeedProfileMigrationProviderUncertain(FeedProfileMigrationError):
    """A one-shot POST crossed its durable boundary and needs reconciliation."""

    code = 'provider_outcome_uncertain'


@dataclass(frozen=True)
class ProfileMigrationResult:
    """Strict allowlist safe for command JSON output and audit records."""

    phase: str
    dry_run: bool
    account_id: int
    tenant_id: int
    state: str
    revision: int
    source_fingerprint: str
    target_fingerprint: str
    verification_outcome: str
    owned_feed_count: int
    foreign_feed_count: int
    parity_verified: bool
    settlement_remaining_seconds: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def feed_profile_writer_lock_identity(account_id: int) -> str:
    """Return the shared logical writer identity used by rollout controls.

    AVT-002d2 does not hold a database/session lock over provider HTTP.  The
    identity remains public so legacy onboarding and operational telemetry use
    one stable coordination label while the mutually-exclusive production
    writer flag is enforced.
    """

    account_id = _positive_id(account_id, name='account_id')
    return f'marketplace-feed-profile-writer:{account_id}'


def _positive_id(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{name} must be a positive integer.')
    return value


def _valid_revision(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_REVISION
    ):
        raise ValueError('expected_revision must be a bounded non-negative integer.')
    return value


def _valid_fingerprint(value: object, *, optional: bool) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(
            'expected_source_fingerprint must be a lowercase SHA-256 value.',
        )
    return value


def _require_mutation_expectations(
    expected_revision: int | None,
    expected_source_fingerprint: str | None,
) -> tuple[int, str]:
    if expected_revision is None or expected_source_fingerprint is None:
        raise ValueError(
            'Mutating profile migration requires exact revision and fingerprint.',
        )
    return expected_revision, expected_source_fingerprint


def _migration_enabled() -> bool:
    return getattr(
        settings,
        'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED',
        False,
    ) is True


def _source_settlement_seconds() -> int:
    value = getattr(
        settings,
        'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS',
        3600,
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 300 <= value <= 86_400
    ):
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed source settlement interval is invalid.',
        )
    return value


def _require_new_writer_enabled() -> None:
    if not _migration_enabled():
        raise FeedProfileMigrationConflict(
            'Marketplace feed profile migration writer is disabled.',
        )


def _load_account(tenant_id: int, account_id: int) -> MarketplaceAccount:
    account = (
        MarketplaceAccount.all_objects.select_related('tenant')
        .filter(pk=account_id, tenant_id=tenant_id)
        .first()
    )
    if account is None:
        raise FeedProfileMigrationConflict(
            'Marketplace account does not match the requested tenant scope.',
        )
    _assert_account_live(account)
    if account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
        raise FeedProfileMigrationSafetyError(
            'Marketplace account provider is not supported by this migration.',
        )
    return account


def _assert_account_live(account: MarketplaceAccount) -> None:
    if (
        account.deleted_at is not None
        or not account.is_active
        or not account.tenant.is_active
    ):
        raise FeedProfileMigrationConflict(
            'Marketplace account owner is not live.',
        )


def _load_endpoint(account: MarketplaceAccount) -> MarketplaceFeedEndpoint | None:
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_related('account', 'account__tenant')
        .filter(account_id=account.pk)
        .first()
    )
    if endpoint is not None:
        _assert_endpoint_owner(account, endpoint)
    return endpoint


def _assert_endpoint_owner(
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
) -> None:
    _assert_account_live(account)
    if endpoint.account_id != account.pk or endpoint.account.tenant_id != account.tenant_id:
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint owner scope changed.',
        )
    current_digest = account_identity_digest(account)
    endpoint_digest = str(endpoint.owner_identity_digest or '')
    if not hmac.compare_digest(current_digest, endpoint_digest):
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint owner identity changed.',
        )
    if endpoint.storage_mode != MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE:
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed endpoint storage mode is not migratable.',
        )
    if not 0 <= endpoint.profile_revision <= _MAX_REVISION:
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed endpoint revision is invalid.',
        )


def _assert_expected(
    *,
    revision: int,
    fingerprint: str,
    expected_revision: int,
    expected_fingerprint: str,
) -> None:
    if revision != expected_revision or not hmac.compare_digest(
        fingerprint,
        expected_fingerprint,
    ):
        raise FeedProfileMigrationConflict(
            'Marketplace feed profile revision or fingerprint changed.',
        )


def _next_revision(current: int) -> int:
    if current >= _MAX_REVISION:
        raise FeedProfileMigrationConflict(
            'Marketplace feed profile revision is exhausted.',
        )
    return current + 1


def _primary_signing_key_id() -> str:
    key_id = str(
        getattr(settings, 'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID', '') or '',
    )
    keyring = getattr(settings, 'MARKETPLACE_FEED_URL_SIGNING_KEYS', {})
    if (
        _KEY_ID_RE.fullmatch(key_id) is None
        or not isinstance(keyring, Mapping)
        or key_id not in keyring
        or not isinstance(keyring[key_id], bytes)
        or not 32 <= len(keyring[key_id]) <= 1024
    ):
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed signing key is not safely configured.',
        )
    try:
        canonical_marketplace_feed_public_base_url(
            getattr(settings, 'MARKETPLACE_FEED_PUBLIC_BASE_URL', ''),
        )
    except FeedEndpointConfigurationError:
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed public base URL is not safely configured.',
        ) from None
    return key_id


def _source_plan_without_capability(
    endpoint: MarketplaceFeedEndpoint,
    profile: object,
) -> AvitoProfilePlan:
    plan = inspect_unprovisioned_profile(endpoint.account, profile)
    if (
        plan.source_url != endpoint.legacy_profile_url
        or plan.source_object_key != endpoint.legacy_object_key
        or not hmac.compare_digest(
            plan.source_fingerprint,
            endpoint.profile_fingerprint,
        )
    ):
        raise FeedProfileMigrationSafetyError(
            'Avito source profile does not match the frozen endpoint baseline.',
        )
    return plan


def _result(
    *,
    phase: str,
    apply: bool,
    account: MarketplaceAccount,
    state: str,
    revision: int,
    source_fingerprint: str,
    target_fingerprint: str = '',
    verification_outcome: str,
    owned_feed_count: int,
    foreign_feed_count: int,
    parity_verified: bool = False,
    settlement_remaining_seconds: int = 0,
) -> ProfileMigrationResult:
    return ProfileMigrationResult(
        phase=phase,
        dry_run=not apply,
        account_id=account.pk,
        tenant_id=account.tenant_id,
        state=state,
        revision=revision,
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
        verification_outcome=verification_outcome,
        owned_feed_count=owned_feed_count,
        foreign_feed_count=foreign_feed_count,
        parity_verified=parity_verified,
        settlement_remaining_seconds=settlement_remaining_seconds,
    )


def _locked_account_and_endpoint(
    tenant_id: int,
    account_id: int,
) -> tuple[MarketplaceAccount, MarketplaceFeedEndpoint | None]:
    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .filter(pk=account_id, tenant_id=tenant_id)
        .first()
    )
    if account is None:
        raise FeedProfileMigrationConflict(
            'Marketplace account does not match the requested tenant scope.',
        )
    _assert_account_live(account)
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .select_related('account', 'account__tenant')
        .filter(account_id=account_id)
        .first()
    )
    if endpoint is not None:
        _assert_endpoint_owner(account, endpoint)
    return account, endpoint


def _enter_migrating(
    *,
    account: MarketplaceAccount,
    observed_endpoint: MarketplaceFeedEndpoint | None,
    plan: AvitoProfilePlan,
    expected_owner_digest: str,
    expected_revision: int,
    expected_fingerprint: str,
) -> MarketplaceFeedEndpoint:
    """Provision or resume the pre-POST bridge probe checkpoint."""

    with transaction.atomic():
        locked_account, endpoint = _locked_account_and_endpoint(
            account.tenant_id,
            account.pk,
        )
        if not hmac.compare_digest(
            account_identity_digest(locked_account),
            expected_owner_digest,
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace account identity changed during profile inspection.',
            )
        if observed_endpoint is None:
            if endpoint is not None:
                raise FeedProfileMigrationConflict(
                    'Marketplace feed endpoint appeared during profile inspection.',
                )
        elif endpoint is None or not _same_endpoint_generation(
            endpoint,
            observed_endpoint,
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint generation changed during profile inspection.',
            )
        if endpoint is None:
            _assert_expected(
                revision=0,
                fingerprint=plan.source_fingerprint,
                expected_revision=expected_revision,
                expected_fingerprint=expected_fingerprint,
            )
            endpoint = MarketplaceFeedEndpoint.objects.create(
                account=locked_account,
                token_key_id=_primary_signing_key_id(),
                owner_identity_digest=account_identity_digest(locked_account),
                serve_enabled=True,
                storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
                legacy_object_key=plan.source_object_key,
                legacy_profile_url=plan.source_url,
                profile_state=MarketplaceFeedEndpoint.ProfileState.MIGRATING,
                profile_fingerprint=plan.source_fingerprint,
                profile_revision=1,
                profile_verified_at=timezone.now(),
            )
        elif endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW:
            _assert_expected(
                revision=endpoint.profile_revision,
                fingerprint=plan.source_fingerprint,
                expected_revision=expected_revision,
                expected_fingerprint=expected_fingerprint,
            )
            endpoint.serve_enabled = True
            endpoint.legacy_object_key = plan.source_object_key
            endpoint.legacy_profile_url = plan.source_url
            endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.MIGRATING
            endpoint.profile_fingerprint = plan.source_fingerprint
            endpoint.profile_revision = _next_revision(endpoint.profile_revision)
            endpoint.profile_verified_at = timezone.now()
            endpoint.save(update_fields=(
                'serve_enabled',
                'legacy_object_key',
                'legacy_profile_url',
                'profile_state',
                'profile_fingerprint',
                'profile_revision',
                'profile_verified_at',
                'updated_at',
            ))
        elif endpoint.profile_state in {
            MarketplaceFeedEndpoint.ProfileState.MIGRATING,
            MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
        }:
            _assert_expected(
                revision=endpoint.profile_revision,
                fingerprint=endpoint.profile_fingerprint,
                expected_revision=expected_revision,
                expected_fingerprint=expected_fingerprint,
            )
            if (
                not hmac.compare_digest(endpoint.profile_fingerprint, plan.source_fingerprint)
                or endpoint.legacy_object_key != plan.source_object_key
                or endpoint.legacy_profile_url != plan.source_url
                or not endpoint.serve_enabled
            ):
                raise FeedProfileMigrationConflict(
                    'Marketplace feed bridge checkpoint changed.',
                )
        else:
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint is past the prepare phase.',
            )

    return endpoint


def _finish_bridge_ready(
    *,
    endpoint: MarketplaceFeedEndpoint,
    source_fingerprint: str,
) -> MarketplaceFeedEndpoint:
    with transaction.atomic():
        _locked_account, locked = _locked_account_and_endpoint(
            endpoint.account.tenant_id,
            endpoint.account_id,
        )
        if locked is None:
            raise FeedProfileMigrationConflict(
                'Marketplace feed bridge checkpoint disappeared.',
            )
        if (
            locked.profile_state
            not in {
                MarketplaceFeedEndpoint.ProfileState.MIGRATING,
                MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            }
            or not _same_endpoint_generation(locked, endpoint)
            or not hmac.compare_digest(
                locked.profile_fingerprint,
                source_fingerprint,
            )
            or not locked.serve_enabled
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed bridge checkpoint changed during parity proof.',
            )
        if locked.profile_state == MarketplaceFeedEndpoint.ProfileState.MIGRATING:
            locked.profile_state = MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
            locked.profile_revision = _next_revision(locked.profile_revision)
            locked.profile_verified_at = timezone.now()
            locked.save(update_fields=(
                'profile_state',
                'profile_revision',
                'profile_verified_at',
                'updated_at',
            ))
    return locked


def _enter_update_unknown(
    *,
    endpoint: MarketplaceFeedEndpoint,
    expected_revision: int,
    expected_fingerprint: str,
) -> MarketplaceFeedEndpoint:
    with transaction.atomic():
        _locked_account, locked = _locked_account_and_endpoint(
            endpoint.account.tenant_id,
            endpoint.account_id,
        )
        if locked is None:
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint disappeared before provider update.',
            )
        if locked.profile_state != MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY:
            raise FeedProfileMigrationConflict(
                'Marketplace feed profile POST boundary was already claimed.',
            )
        _assert_expected(
            revision=locked.profile_revision,
            fingerprint=locked.profile_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_fingerprint,
        )
        if not _same_endpoint_generation(locked, endpoint) or not locked.serve_enabled:
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint generation changed before provider update.',
            )
        boundary_at = timezone.now()
        baseline_verified_at = locked.profile_verified_at
        if baseline_verified_at is None:
            raise FeedProfileMigrationSafetyError(
                'Marketplace feed source baseline timestamp is missing.',
            )
        if baseline_verified_at >= boundary_at:
            baseline_verified_at = boundary_at - timedelta(
                microseconds=1,
            )
        next_revision = _next_revision(locked.profile_revision)
        updated = MarketplaceFeedEndpoint.objects.filter(
            pk=locked.pk,
            profile_state=MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            profile_revision=locked.profile_revision,
        ).update(
            profile_state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
            profile_revision=next_revision,
            profile_verified_at=baseline_verified_at,
            updated_at=boundary_at,
        )
        if updated != 1:
            raise FeedProfileMigrationConflict(
                'Marketplace feed profile POST boundary was already claimed.',
            )
        locked.profile_state = MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
        locked.profile_revision = next_revision
        locked.profile_verified_at = baseline_verified_at
        locked.updated_at = boundary_at
    return locked


def _inspect_phase(
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint | None,
    client: AvitoProfileMigrationClient,
) -> ProfileMigrationResult:
    profile = client.get_profile()
    if endpoint is None or endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW:
        plan = inspect_unprovisioned_profile(account, profile)
        return _result(
            phase='inspect',
            apply=False,
            account=account,
            state=MarketplaceFeedEndpoint.ProfileState.NEW,
            revision=endpoint.profile_revision if endpoint is not None else 0,
            source_fingerprint=plan.source_fingerprint,
            verification_outcome='source_confirmed',
            owned_feed_count=plan.owned_feed_count,
            foreign_feed_count=plan.foreign_feed_count,
        )
    if endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    }:
        plan = _source_plan_without_capability(endpoint, profile)
        return _result(
            phase='inspect',
            apply=False,
            account=account,
            state=endpoint.profile_state,
            revision=endpoint.profile_revision,
            source_fingerprint=plan.source_fingerprint,
            verification_outcome='source_confirmed',
            owned_feed_count=plan.owned_feed_count,
            foreign_feed_count=plan.foreign_feed_count,
        )
    if endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        MarketplaceFeedEndpoint.ProfileState.VERIFIED,
    }:
        observation = observe_endpoint_profile(endpoint, profile)
        return _result(
            phase='inspect',
            apply=False,
            account=account,
            state=endpoint.profile_state,
            revision=endpoint.profile_revision,
            source_fingerprint=observation.source_fingerprint,
            target_fingerprint=observation.target_fingerprint,
            verification_outcome=f'{observation.outcome}_confirmed',
            owned_feed_count=observation.owned_feed_count,
            foreign_feed_count=observation.foreign_feed_count,
        )
    raise FeedProfileMigrationConflict(
        'Marketplace feed endpoint requires manual review.',
    )


def _prepare_phase(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint | None,
    client: AvitoProfileMigrationClient,
    apply: bool,
    expected_revision: int | None,
    expected_source_fingerprint: str | None,
) -> ProfileMigrationResult:
    if apply:
        _require_new_writer_enabled()
        expected_revision, expected_source_fingerprint = (
            _require_mutation_expectations(
                expected_revision,
                expected_source_fingerprint,
            )
        )
    inspected_owner_digest = account_identity_digest(account)
    profile = client.get_profile()
    state: str
    if endpoint is None or endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW:
        plan = inspect_unprovisioned_profile(account, profile)
        revision = endpoint.profile_revision if endpoint is not None else 0
        state = MarketplaceFeedEndpoint.ProfileState.NEW
    elif endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    }:
        plan = _source_plan_without_capability(endpoint, profile)
        revision = endpoint.profile_revision
        state = endpoint.profile_state
    else:
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint is past the prepare phase.',
        )
    # Preparation must not create a sticky bridge checkpoint for a profile
    # that the existing-profile POST schema cannot round-trip later.  Inspect
    # remains available so an operator can repair the provider profile first.
    validate_avito_profile_upsert_target(plan.source_profile)
    if not apply:
        return _result(
            phase='prepare',
            apply=False,
            account=account,
            state=state,
            revision=revision,
            source_fingerprint=plan.source_fingerprint,
            verification_outcome='ready_for_bridge_probe',
            owned_feed_count=plan.owned_feed_count,
            foreign_feed_count=plan.foreign_feed_count,
        )

    assert expected_revision is not None
    assert expected_source_fingerprint is not None
    endpoint = _enter_migrating(
        account=account,
        observed_endpoint=endpoint,
        plan=plan,
        expected_owner_digest=inspected_owner_digest,
        expected_revision=expected_revision,
        expected_fingerprint=expected_source_fingerprint,
    )
    try:
        stable_url = marketplace_feed_public_url(endpoint)
        probe_feed_bridge_parity(endpoint)
        latest_profile = client.get_profile()
        latest_plan = _source_plan_without_capability(endpoint, latest_profile)
        observation = observe_endpoint_profile(endpoint, latest_profile)
    except (AvitoProfileMigrationError, FeedEndpointConfigurationError) as exc:
        if isinstance(exc, AvitoProfileValidationError):
            raise FeedProfileMigrationSafetyError(str(exc)) from None
        raise FeedProfileMigrationError(
            'Marketplace feed bridge preparation failed safely.',
        ) from None
    if (
        observation.outcome != 'source'
        or not hmac.compare_digest(
            latest_plan.source_fingerprint,
            plan.source_fingerprint,
        )
        or not stable_url
    ):
        raise FeedProfileMigrationSafetyError(
            'Avito profile changed during bridge preparation.',
        )
    endpoint = _finish_bridge_ready(
        endpoint=endpoint,
        source_fingerprint=latest_plan.source_fingerprint,
    )
    return _result(
        phase='prepare',
        apply=True,
        account=endpoint.account,
        state=endpoint.profile_state,
        revision=endpoint.profile_revision,
        source_fingerprint=observation.source_fingerprint,
        target_fingerprint=observation.target_fingerprint,
        verification_outcome='bridge_parity_verified',
        owned_feed_count=observation.owned_feed_count,
        foreign_feed_count=observation.foreign_feed_count,
        parity_verified=True,
    )


def _migrate_phase(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint | None,
    client: AvitoProfileMigrationClient,
    apply: bool,
    expected_revision: int | None,
    expected_source_fingerprint: str | None,
) -> ProfileMigrationResult:
    if endpoint is None or endpoint.profile_state != MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY:
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint is not ready for profile migration.',
        )
    if apply:
        _require_new_writer_enabled()
        expected_revision, expected_source_fingerprint = (
            _require_mutation_expectations(
                expected_revision,
                expected_source_fingerprint,
            )
        )
        _assert_expected(
            revision=endpoint.profile_revision,
            fingerprint=endpoint.profile_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )

    profile = client.get_profile()
    source_plan = _source_plan_without_capability(endpoint, profile)
    # URL replacement does not change report_email or any other top-level
    # upsert field, so this catches wire-incompatible source profiles without
    # deriving capability material during dry-run.
    validate_avito_profile_upsert_target(source_plan.source_profile)
    if not apply:
        # Dry-run deliberately never derives capability material.
        return _result(
            phase='migrate',
            apply=False,
            account=account,
            state=endpoint.profile_state,
            revision=endpoint.profile_revision,
            source_fingerprint=source_plan.source_fingerprint,
            verification_outcome='ready_for_one_shot_post',
            owned_feed_count=source_plan.owned_feed_count,
            foreign_feed_count=source_plan.foreign_feed_count,
            parity_verified=True,
        )

    assert expected_revision is not None
    assert expected_source_fingerprint is not None
    try:
        observation = observe_endpoint_profile(endpoint, profile)
        if observation.outcome != 'source':
            raise AvitoProfileValidationError(
                'Avito profile is not at the exact source baseline.',
            )
        validate_avito_profile_upsert_target(observation.plan.target_profile)
        latest_profile = client.get_profile()
        latest = observe_endpoint_profile(endpoint, latest_profile)
        if latest.outcome != 'source' or not hmac.compare_digest(
            latest.source_fingerprint,
            observation.source_fingerprint,
        ):
            raise AvitoProfileValidationError(
                'Avito profile changed before the one-shot boundary.',
            )
        validate_avito_profile_upsert_target(latest.plan.target_profile)
        # Fetch/refresh the exact bearer and consume POST admission only after
        # the last safe GET.  A GET-side 401 refresh must not leave us holding
        # an obsolete token for the one-shot request.
        prepared = client.prepare_post()
    except AvitoProfileValidationError as exc:
        raise FeedProfileMigrationSafetyError(str(exc)) from None
    except AvitoProfileMigrationError:
        raise FeedProfileMigrationError(
            'Avito profile migration preflight failed safely.',
        ) from None

    boundary = _enter_update_unknown(
        endpoint=endpoint,
        expected_revision=expected_revision,
        expected_fingerprint=expected_source_fingerprint,
    )
    try:
        client.post_profile_once(prepared, latest.plan.target_profile)
    except AvitoProfilePostError:
        raise FeedProfileMigrationProviderUncertain(
            'Avito profile update crossed UPDATE_UNKNOWN; run GET-only reconciliation.',
        ) from None
    except Exception:
        raise FeedProfileMigrationProviderUncertain(
            'Avito profile update crossed UPDATE_UNKNOWN; run GET-only reconciliation.',
        ) from None

    return _result(
        phase='migrate',
        apply=True,
        account=boundary.account,
        state=boundary.profile_state,
        revision=boundary.profile_revision,
        source_fingerprint=latest.source_fingerprint,
        target_fingerprint=latest.target_fingerprint,
        verification_outcome='post_submitted_unverified',
        owned_feed_count=latest.owned_feed_count,
        foreign_feed_count=latest.foreign_feed_count,
        parity_verified=True,
    )


def _same_endpoint_generation(
    locked: MarketplaceFeedEndpoint,
    observed: MarketplaceFeedEndpoint,
) -> bool:
    return (
        locked.public_id == observed.public_id
        and locked.account_id == observed.account_id
        and locked.token_key_id == observed.token_key_id
        and locked.previous_token_key_id == observed.previous_token_key_id
        and hmac.compare_digest(
            locked.owner_identity_digest,
            observed.owner_identity_digest,
        )
        and locked.capability_revision == observed.capability_revision
        and locked.serve_enabled == observed.serve_enabled
        and locked.storage_mode == observed.storage_mode
        and locked.legacy_object_key == observed.legacy_object_key
        and locked.legacy_profile_url == observed.legacy_profile_url
        and locked.profile_state == observed.profile_state
        and hmac.compare_digest(
            locked.profile_fingerprint,
            observed.profile_fingerprint,
        )
        and locked.profile_revision == observed.profile_revision
        and locked.profile_verified_at == observed.profile_verified_at
        and locked.updated_at == observed.updated_at
    )


def _apply_exact_target(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
    observation,
    expected_revision: int,
    expected_source_fingerprint: str,
) -> tuple[MarketplaceAccount, MarketplaceFeedEndpoint]:
    """Apply one exact GET-only target proof under account/endpoint CAS."""

    if observation.outcome != 'target':
        raise FeedProfileMigrationSafetyError(
            'Avito profile reconciliation did not prove the exact target.',
        )
    with transaction.atomic():
        locked_account, locked = _locked_account_and_endpoint(
            account.tenant_id,
            account.pk,
        )
        if (
            locked is None
            or locked.profile_state
            not in {
                MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
                MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            }
            or locked.profile_state != endpoint.profile_state
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint reconciliation state changed.',
            )
        _assert_expected(
            revision=locked.profile_revision,
            fingerprint=locked.profile_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )
        if (
            not _same_endpoint_generation(locked, endpoint)
            or not locked.serve_enabled
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint generation changed during reconciliation.',
            )
        # An exact target proof uses the current capability URL.  A previous
        # rotation key is therefore no longer needed and VERIFIED forbids it.
        locked.previous_token_key_id = ''
        locked.profile_state = MarketplaceFeedEndpoint.ProfileState.VERIFIED
        locked.profile_fingerprint = observation.target_fingerprint
        locked.profile_revision = _next_revision(locked.profile_revision)
        locked.profile_verified_at = timezone.now()
        locked.save(update_fields=(
            'previous_token_key_id',
            'profile_state',
            'profile_fingerprint',
            'profile_revision',
            'profile_verified_at',
            'updated_at',
        ))
    return locked_account, locked


def _reconcile_phase(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint | None,
    client: AvitoProfileMigrationClient,
    apply: bool,
    expected_revision: int | None,
    expected_source_fingerprint: str | None,
) -> ProfileMigrationResult:
    if endpoint is None or endpoint.profile_state not in {
        MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    }:
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint has no reconcilable profile update.',
        )
    if apply:
        expected_revision, expected_source_fingerprint = (
            _require_mutation_expectations(
                expected_revision,
                expected_source_fingerprint,
            )
        )
        _assert_expected(
            revision=endpoint.profile_revision,
            fingerprint=endpoint.profile_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )
    profile = client.get_profile()
    observation = observe_endpoint_profile(endpoint, profile)
    if not apply:
        return _result(
            phase='reconcile',
            apply=False,
            account=account,
            state=endpoint.profile_state,
            revision=endpoint.profile_revision,
            source_fingerprint=observation.source_fingerprint,
            target_fingerprint=observation.target_fingerprint,
            verification_outcome=f'{observation.outcome}_confirmed',
            owned_feed_count=observation.owned_feed_count,
            foreign_feed_count=observation.foreign_feed_count,
            parity_verified=True,
        )

    assert expected_revision is not None
    assert expected_source_fingerprint is not None
    if observation.outcome == 'source':
        with transaction.atomic():
            locked_account, locked = _locked_account_and_endpoint(
                account.tenant_id,
                account.pk,
            )
            if (
                locked is None
                or locked.profile_state != endpoint.profile_state
                or locked.profile_state
                not in {
                    MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
                    MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
                }
            ):
                raise FeedProfileMigrationConflict(
                    'Marketplace feed endpoint reconciliation state changed.',
                )
            _assert_expected(
                revision=locked.profile_revision,
                fingerprint=locked.profile_fingerprint,
                expected_revision=expected_revision,
                expected_fingerprint=expected_source_fingerprint,
            )
            if (
                not _same_endpoint_generation(locked, endpoint)
                or not locked.serve_enabled
            ):
                raise FeedProfileMigrationConflict(
                    'Marketplace feed endpoint generation changed during reconciliation.',
                )
        return _result(
            phase='reconcile',
            apply=True,
            account=locked_account,
            state=locked.profile_state,
            revision=locked.profile_revision,
            source_fingerprint=observation.source_fingerprint,
            target_fingerprint=observation.target_fingerprint,
            verification_outcome=(
                'source_confirmed_update_unknown'
                if endpoint.profile_state
                == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
                else 'source_confirmed_bridge_ready'
            ),
            owned_feed_count=observation.owned_feed_count,
            foreign_feed_count=observation.foreign_feed_count,
            parity_verified=True,
        )

    locked_account, locked = _apply_exact_target(
        account=account,
        endpoint=endpoint,
        observation=observation,
        expected_revision=expected_revision,
        expected_source_fingerprint=expected_source_fingerprint,
    )
    return _result(
        phase='reconcile',
        apply=True,
        account=locked_account,
        state=locked.profile_state,
        revision=locked.profile_revision,
        source_fingerprint=observation.source_fingerprint,
        target_fingerprint=observation.target_fingerprint,
        verification_outcome='target_verified',
        owned_feed_count=observation.owned_feed_count,
        foreign_feed_count=observation.foreign_feed_count,
        parity_verified=True,
    )


def _source_observation_is_persisted(endpoint: MarketplaceFeedEndpoint) -> bool:
    return (
        endpoint.profile_verified_at is not None
        and endpoint.updated_at is not None
        and endpoint.profile_verified_at > endpoint.updated_at
    )


def _seconds_until(moment, *, now) -> int:
    # Operator output stays bounded even if a database timestamp is corrupted
    # into the future.  A positive value still refuses the transition.
    return min(86_400, max(0, math.ceil((moment - now).total_seconds())))


def _source_resolution_remaining(
    endpoint: MarketplaceFeedEndpoint,
    *,
    now,
    settlement_seconds: int,
) -> int:
    boundary_due = endpoint.updated_at + timedelta(seconds=settlement_seconds)
    verified_at = endpoint.profile_verified_at
    if verified_at is None or not _source_observation_is_persisted(endpoint):
        return _seconds_until(boundary_due, now=now)
    second_due = verified_at + timedelta(
        seconds=settlement_seconds,
    )
    # Keep the boundary-age requirement explicit even though a valid first
    # observation is itself admitted only after one boundary interval.
    two_boundary_intervals = endpoint.updated_at + timedelta(
        seconds=2 * settlement_seconds,
    )
    return max(
        _seconds_until(second_due, now=now),
        _seconds_until(two_boundary_intervals, now=now),
    )


def _resolve_source_phase(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint | None,
    client: AvitoProfileMigrationClient,
    apply: bool,
    expected_revision: int | None,
    expected_source_fingerprint: str | None,
) -> ProfileMigrationResult:
    """Resolve a settled exact source using GETs only; never issue a POST."""

    if (
        endpoint is None
        or endpoint.profile_state
        != MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    ):
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint has no uncertain profile update.',
        )
    settlement_seconds = _source_settlement_seconds()
    if apply:
        expected_revision, expected_source_fingerprint = (
            _require_mutation_expectations(
                expected_revision,
                expected_source_fingerprint,
            )
        )
        _assert_expected(
            revision=endpoint.profile_revision,
            fingerprint=endpoint.profile_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )

    # Eligibility belongs to the start of the provider GET.  A slow request
    # that began before a due time must not cross that threshold merely by
    # completing (or later waiting for a database lock) after it.
    request_started_at = timezone.now()
    profile = client.get_profile()
    observation = observe_endpoint_profile(endpoint, profile)
    # Persist the completion of an exact validated observation as the marker,
    # while retaining request_started_at for every due-time decision below.
    completed_at = timezone.now()
    if observation.outcome == 'target':
        if not apply:
            return _result(
                phase='resolve-source',
                apply=False,
                account=account,
                state=endpoint.profile_state,
                revision=endpoint.profile_revision,
                source_fingerprint=observation.source_fingerprint,
                target_fingerprint=observation.target_fingerprint,
                verification_outcome='target_confirmed',
                owned_feed_count=observation.owned_feed_count,
                foreign_feed_count=observation.foreign_feed_count,
                parity_verified=True,
            )
        assert expected_revision is not None
        assert expected_source_fingerprint is not None
        target_account, target_endpoint = _apply_exact_target(
            account=account,
            endpoint=endpoint,
            observation=observation,
            expected_revision=expected_revision,
            expected_source_fingerprint=expected_source_fingerprint,
        )
        return _result(
            phase='resolve-source',
            apply=True,
            account=target_account,
            state=target_endpoint.profile_state,
            revision=target_endpoint.profile_revision,
            source_fingerprint=observation.source_fingerprint,
            target_fingerprint=observation.target_fingerprint,
            verification_outcome='target_verified',
            owned_feed_count=observation.owned_feed_count,
            foreign_feed_count=observation.foreign_feed_count,
            parity_verified=True,
        )
    if observation.outcome != 'source':
        raise FeedProfileMigrationSafetyError(
            'Avito profile source resolution did not prove an exact profile.',
        )

    remaining = _source_resolution_remaining(
        endpoint,
        now=request_started_at,
        settlement_seconds=settlement_seconds,
    )
    marker_persisted = _source_observation_is_persisted(endpoint)
    if not apply:
        if remaining:
            outcome = 'source_settlement_pending'
        elif marker_persisted:
            outcome = 'source_ready_for_resolution'
        else:
            outcome = 'source_observation_ready'
        return _result(
            phase='resolve-source',
            apply=False,
            account=account,
            state=endpoint.profile_state,
            revision=endpoint.profile_revision,
            source_fingerprint=observation.source_fingerprint,
            target_fingerprint=observation.target_fingerprint,
            verification_outcome=outcome,
            owned_feed_count=observation.owned_feed_count,
            foreign_feed_count=observation.foreign_feed_count,
            parity_verified=True,
            settlement_remaining_seconds=remaining,
        )

    assert expected_revision is not None
    assert expected_source_fingerprint is not None
    with transaction.atomic():
        locked_account, locked = _locked_account_and_endpoint(
            account.tenant_id,
            account.pk,
        )
        if (
            locked is None
            or locked.profile_state
            != MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed source resolution state changed.',
            )
        _assert_expected(
            revision=locked.profile_revision,
            fingerprint=locked.profile_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )
        if (
            not _same_endpoint_generation(locked, endpoint)
            or not locked.serve_enabled
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed endpoint generation changed during source resolution.',
            )

        remaining = _source_resolution_remaining(
            locked,
            now=request_started_at,
            settlement_seconds=settlement_seconds,
        )
        marker_persisted = _source_observation_is_persisted(locked)
        if remaining:
            outcome = 'source_settlement_pending'
        elif not marker_persisted:
            next_revision = _next_revision(locked.profile_revision)
            updated = MarketplaceFeedEndpoint.objects.filter(
                pk=locked.pk,
                profile_state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
                profile_revision=locked.profile_revision,
                updated_at=locked.updated_at,
                profile_verified_at=locked.profile_verified_at,
            ).update(
                profile_verified_at=completed_at,
                profile_revision=next_revision,
            )
            if updated != 1:
                raise FeedProfileMigrationConflict(
                    'Marketplace feed source observation checkpoint changed.',
                )
            locked.profile_verified_at = completed_at
            locked.profile_revision = next_revision
            remaining = settlement_seconds
            outcome = 'source_observation_recorded'
        else:
            locked.previous_token_key_id = ''
            locked.profile_state = MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
            locked.profile_revision = _next_revision(locked.profile_revision)
            locked.profile_verified_at = completed_at
            locked.save(update_fields=(
                'previous_token_key_id',
                'profile_state',
                'profile_revision',
                'profile_verified_at',
                'updated_at',
            ))
            remaining = 0
            outcome = 'source_resolved'

    return _result(
        phase='resolve-source',
        apply=True,
        account=locked_account,
        state=locked.profile_state,
        revision=locked.profile_revision,
        source_fingerprint=observation.source_fingerprint,
        target_fingerprint=observation.target_fingerprint,
        verification_outcome=outcome,
        owned_feed_count=observation.owned_feed_count,
        foreign_feed_count=observation.foreign_feed_count,
        parity_verified=True,
        settlement_remaining_seconds=remaining,
    )


def _confirm_prepare_source_phase(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint | None,
    client: AvitoProfileMigrationClient,
    apply: bool,
    expected_revision: int | None,
    expected_source_fingerprint: str | None,
) -> ProfileMigrationResult:
    if endpoint is None or endpoint.profile_state not in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    }:
        raise FeedProfileMigrationConflict(
            'Marketplace feed endpoint has no confirmable prepare checkpoint.',
        )
    profile = client.get_profile()
    plan = _source_plan_without_capability(endpoint, profile)
    if apply:
        expected_revision, expected_source_fingerprint = (
            _require_mutation_expectations(
                expected_revision,
                expected_source_fingerprint,
            )
        )
        _assert_expected(
            revision=endpoint.profile_revision,
            fingerprint=plan.source_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )
    if not apply:
        return _result(
            phase='confirm-prepare-source',
            apply=False,
            account=account,
            state=endpoint.profile_state,
            revision=endpoint.profile_revision,
            source_fingerprint=plan.source_fingerprint,
            verification_outcome='ready_to_confirm_prepare_source',
            owned_feed_count=plan.owned_feed_count,
            foreign_feed_count=plan.foreign_feed_count,
        )

    assert expected_revision is not None
    assert expected_source_fingerprint is not None
    with transaction.atomic():
        locked_account, locked = _locked_account_and_endpoint(
            account.tenant_id,
            account.pk,
        )
        if locked is None or locked.profile_state not in {
            MarketplaceFeedEndpoint.ProfileState.MIGRATING,
            MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
        }:
            raise FeedProfileMigrationConflict(
                'Marketplace feed prepare checkpoint changed.',
            )
        _assert_expected(
            revision=locked.profile_revision,
            fingerprint=plan.source_fingerprint,
            expected_revision=expected_revision,
            expected_fingerprint=expected_source_fingerprint,
        )
        if (
            not _same_endpoint_generation(locked, endpoint)
            or not locked.serve_enabled
            or locked.legacy_object_key != plan.source_object_key
            or locked.legacy_profile_url != plan.source_url
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed prepare generation changed.',
            )
        if locked.profile_state == MarketplaceFeedEndpoint.ProfileState.MIGRATING:
            # This is an operator checkpoint, not parity proof.  A failed or
            # crashed probe must remain MIGRATING so only a later full prepare
            # (direct/stable/direct + provider re-GET) can make it ready.
            locked.profile_revision = _next_revision(locked.profile_revision)
            locked.save(update_fields=(
                'profile_revision',
                'updated_at',
            ))
        revision = locked.profile_revision
        state = locked.profile_state
    return _result(
        phase='confirm-prepare-source',
        apply=True,
        account=locked_account,
        state=state,
        revision=revision,
        source_fingerprint=plan.source_fingerprint,
        verification_outcome='source_confirmed_prepare_checkpoint',
        owned_feed_count=plan.owned_feed_count,
        foreign_feed_count=plan.foreign_feed_count,
    )


def run_feed_profile_migration(
    *,
    tenant_id: int,
    account_id: int,
    phase: str,
    expected_revision: int | None = None,
    expected_source_fingerprint: str | None = None,
    apply: bool = False,
) -> ProfileMigrationResult:
    """Run one bounded migration phase for one exact tenant/account pair."""

    tenant_id = _positive_id(tenant_id, name='tenant_id')
    account_id = _positive_id(account_id, name='account_id')
    if not isinstance(phase, str) or phase not in _PHASES:
        raise ValueError('phase is invalid.')
    if not isinstance(apply, bool):
        raise ValueError('apply must be a boolean.')
    expected_revision = _valid_revision(expected_revision, optional=True)
    expected_source_fingerprint = _valid_fingerprint(
        expected_source_fingerprint,
        optional=True,
    )
    if phase == 'inspect' and apply:
        raise ValueError('inspect is always read-only.')

    try:
        account = _load_account(tenant_id, account_id)
        endpoint = _load_endpoint(account)
        client = AvitoProfileMigrationClient(account)
        if phase == 'inspect':
            return _inspect_phase(account, endpoint, client)
        if phase == 'prepare':
            return _prepare_phase(
                account=account,
                endpoint=endpoint,
                client=client,
                apply=apply,
                expected_revision=expected_revision,
                expected_source_fingerprint=expected_source_fingerprint,
            )
        if phase == 'migrate':
            return _migrate_phase(
                account=account,
                endpoint=endpoint,
                client=client,
                apply=apply,
                expected_revision=expected_revision,
                expected_source_fingerprint=expected_source_fingerprint,
            )
        if phase == 'reconcile':
            return _reconcile_phase(
                account=account,
                endpoint=endpoint,
                client=client,
                apply=apply,
                expected_revision=expected_revision,
                expected_source_fingerprint=expected_source_fingerprint,
            )
        if phase == 'resolve-source':
            return _resolve_source_phase(
                account=account,
                endpoint=endpoint,
                client=client,
                apply=apply,
                expected_revision=expected_revision,
                expected_source_fingerprint=expected_source_fingerprint,
            )
        return _confirm_prepare_source_phase(
            account=account,
            endpoint=endpoint,
            client=client,
            apply=apply,
            expected_revision=expected_revision,
            expected_source_fingerprint=expected_source_fingerprint,
        )
    except FeedProfileMigrationError:
        raise
    except AvitoProfileValidationError as exc:
        raise FeedProfileMigrationSafetyError(str(exc)) from None
    except AvitoProfileTransportError:
        raise FeedProfileMigrationError(
            'Avito profile transport failed safely.',
        ) from None
    except FeedEndpointConfigurationError:
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed endpoint configuration is invalid.',
        ) from None


def _assert_fleet_endpoint_owner(
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
) -> None:
    _assert_account_live(account)
    if (
        endpoint.account_id != account.pk
        or endpoint.account.tenant_id != account.tenant_id
        or endpoint.storage_mode
        not in {
            MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
            MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        }
        or not hmac.compare_digest(
            endpoint.owner_identity_digest,
            account_identity_digest(account),
        )
    ):
        raise FeedProfileMigrationConflict(
            'Marketplace feed onboarding owner generation changed.',
        )


def ensure_fleet_feed_endpoint(
    account: MarketplaceAccount,
) -> MarketplaceFeedEndpoint | None:
    """Synchronously reserve a stable endpoint for one future SaaS account."""

    if not private_feed_fleet_enabled():
        return None
    if not isinstance(account, MarketplaceAccount) or account.pk is None:
        raise ValueError('A persisted marketplace account is required.')
    if account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
        return None

    from apps.marketplaces.adapters.avito.adapter import AvitoAdapter

    try:
        locator = AvitoAdapter(account)._legacy_feed_locator()
        key_id = _primary_signing_key_id()
    except Exception:
        raise FeedProfileMigrationSafetyError(
            'Marketplace feed onboarding configuration is invalid.',
        ) from None

    with transaction.atomic():
        locked_account = (
            MarketplaceAccount.all_objects.select_for_update(of=('self',))
            .select_related('tenant')
            .filter(pk=account.pk, tenant_id=account.tenant_id)
            .first()
        )
        if locked_account is None:
            raise FeedProfileMigrationConflict(
                'Marketplace feed onboarding owner disappeared.',
            )
        _assert_account_live(locked_account)
        endpoint = (
            MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
            .select_related('account', 'account__tenant')
            .filter(account_id=locked_account.pk)
            .first()
        )
        if endpoint is not None:
            _assert_fleet_endpoint_owner(locked_account, endpoint)
            return endpoint
        return MarketplaceFeedEndpoint.objects.create(
            account=locked_account,
            token_key_id=key_id,
            owner_identity_digest=account_identity_digest(locked_account),
            serve_enabled=False,
            storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
            legacy_object_key=locator.object_key,
            legacy_profile_url=locator.public_url,
            profile_state=MarketplaceFeedEndpoint.ProfileState.NEW,
        )


def fleet_feed_onboarding_ready(account_id: int) -> bool:
    """Return exact database readiness without provider or storage I/O."""

    if isinstance(account_id, bool) or not isinstance(account_id, int):
        return False
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_related('account', 'account__tenant')
        .filter(account_id=account_id)
        .first()
    )
    if endpoint is None:
        return False
    account = endpoint.account
    try:
        owner_matches = hmac.compare_digest(
            endpoint.owner_identity_digest,
            account_identity_digest(account),
        )
    except Exception:
        return False
    return (
        account.deleted_at is None
        and account.is_active is True
        and account.tenant.is_active is True
        and account.marketplace == MarketplaceAccount.MARKETPLACE_AVITO
        and endpoint.serve_enabled is True
        and endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
        and endpoint.storage_mode
        in {
            MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
            MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        }
        and owner_matches
    )


def _onboarding_observation(endpoint, profile: object):
    return build_profile_plan(
        account=endpoint.account,
        profile=profile,
        source_url=endpoint.legacy_profile_url,
        source_object_key=endpoint.legacy_object_key,
        stable_url=marketplace_feed_public_url(endpoint),
    )


def _enter_onboarding_bridge(
    endpoint: MarketplaceFeedEndpoint,
    *,
    source_fingerprint: str,
) -> MarketplaceFeedEndpoint:
    with transaction.atomic():
        account = (
            MarketplaceAccount.all_objects.select_for_update(of=('self',))
            .select_related('tenant')
            .get(pk=endpoint.account_id)
        )
        locked = (
            MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
            .select_related('account', 'account__tenant')
            .get(pk=endpoint.pk)
        )
        _assert_fleet_endpoint_owner(account, locked)
        if locked.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED:
            return locked
        if locked.profile_state not in {
            MarketplaceFeedEndpoint.ProfileState.NEW,
            MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        }:
            raise FeedProfileMigrationConflict(
                'Marketplace feed onboarding endpoint requires manual review.',
            )
        if (
            locked.profile_state != MarketplaceFeedEndpoint.ProfileState.NEW
            and not hmac.compare_digest(
                locked.profile_fingerprint,
                source_fingerprint,
            )
        ):
            raise FeedProfileMigrationConflict(
                'Marketplace feed onboarding source profile changed.',
            )
        if locked.profile_state != MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY:
            locked.profile_state = MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
            locked.profile_fingerprint = source_fingerprint
            locked.profile_revision = _next_revision(locked.profile_revision)
        locked.serve_enabled = True
        locked.profile_verified_at = timezone.now()
        locked.save(update_fields=(
            'serve_enabled',
            'profile_state',
            'profile_fingerprint',
            'profile_revision',
            'profile_verified_at',
            'updated_at',
        ))
        return locked


def run_fleet_feed_onboarding(
    *,
    tenant_id: int,
    account_id: int,
    report_email: str,
) -> str:
    """Idempotently register and verify the stable URL for one Avito account."""

    if not private_feed_fleet_enabled():
        return 'fleet_disabled'
    if not isinstance(report_email, str) or not report_email:
        raise ValueError('report_email must be non-empty.')
    account = _load_account(
        _positive_id(tenant_id, name='tenant_id'),
        _positive_id(account_id, name='account_id'),
    )
    endpoint = ensure_fleet_feed_endpoint(account)
    if endpoint is None:
        raise FeedProfileMigrationConflict(
            'Marketplace feed onboarding endpoint was not provisioned.',
        )
    _assert_fleet_endpoint_owner(account, endpoint)
    if fleet_feed_onboarding_ready(account.pk):
        return 'already_ready'

    client = AvitoProfileMigrationClient(account)
    try:
        current_profile = client.adapter.get_autoload_profile()
    except NotFoundError:
        current_profile = {}
    except Exception:
        raise FeedProfileMigrationError(
            'Marketplace feed onboarding profile read failed safely.',
        ) from None

    observation = None
    if current_profile:
        try:
            observation = _onboarding_observation(endpoint, current_profile)
        except AvitoProfileValidationError:
            try:
                snapshot = validate_avito_profile_upsert_target(
                    current_profile,
                )
            except AvitoProfileValidationError:
                raise FeedProfileMigrationSafetyError(
                    'Avito onboarding profile is not safely writable.',
                ) from None
            owned_urls = [
                feed['feed_url']
                for feed in snapshot.profile['feeds_data']
                if trusted_account_feed_object_key(
                    account,
                    feed['feed_url'],
                ) is not None
            ]
            if owned_urls:
                raise FeedProfileMigrationSafetyError(
                    'Avito onboarding profile contains a drifting owned feed.',
                )
            observation = None
    if observation is not None and observation.outcome == 'target':
        bridge = _enter_onboarding_bridge(
            endpoint,
            source_fingerprint=observation.source_fingerprint,
        )
        _apply_exact_target(
            account=account,
            endpoint=bridge,
            observation=observation,
            expected_revision=bridge.profile_revision,
            expected_source_fingerprint=bridge.profile_fingerprint,
        )
        return 'verified'

    # UPDATE_UNKNOWN means one physical POST may already have reached Avito.
    # Only an exact GET proof of the target may release that fence; blindly
    # repeating the POST would violate the provider replay guarantee.
    if endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN:
        raise FeedProfileMigrationProviderUncertain(
            'Avito onboarding profile update still requires GET-only reconciliation.',
        )

    stable_url = marketplace_feed_public_url(endpoint)
    if observation is not None:
        target_profile = observation.plan.target_profile
        source_fingerprint = observation.source_fingerprint
    else:
        target_profile = client.adapter._build_autoload_profile_payload(
            current_profile,
            report_email,
            feed_url=stable_url,
            replaced_feed_urls=(endpoint.legacy_profile_url,),
        )
        target = validate_avito_profile_upsert_target(target_profile)
        source_profile = deepcopy(target.profile)
        stable_indexes = [
            index
            for index, feed in enumerate(source_profile['feeds_data'])
            if feed.get('feed_url') == stable_url
        ]
        if len(stable_indexes) != 1:
            raise FeedProfileMigrationSafetyError(
                'Marketplace feed onboarding target is not exact.',
            )
        source_profile['feeds_data'][stable_indexes[0]]['feed_url'] = (
            endpoint.legacy_profile_url
        )
        source = validate_avito_profile_upsert_target(source_profile)
        source_fingerprint = source.fingerprint

    bridge = _enter_onboarding_bridge(
        endpoint,
        source_fingerprint=source_fingerprint,
    )
    if bridge.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED:
        return 'already_ready'
    try:
        prepared = client.prepare_post()
        boundary = _enter_update_unknown(
            endpoint=bridge,
            expected_revision=bridge.profile_revision,
            expected_fingerprint=bridge.profile_fingerprint,
        )
        client.post_profile_once(prepared, target_profile)
        latest_profile = client.get_profile()
        latest = observe_endpoint_profile(boundary, latest_profile)
    except (AvitoProfilePostError, AvitoProfileTransportError):
        raise FeedProfileMigrationProviderUncertain(
            'Avito onboarding profile update requires safe retry.',
        ) from None
    if latest.outcome != 'target':
        raise FeedProfileMigrationProviderUncertain(
            'Avito onboarding profile update is not visible yet.',
        )
    _apply_exact_target(
        account=account,
        endpoint=boundary,
        observation=latest,
        expected_revision=boundary.profile_revision,
        expected_source_fingerprint=boundary.profile_fingerprint,
    )
    return 'verified'
