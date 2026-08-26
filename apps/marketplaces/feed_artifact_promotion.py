"""Atomic promotion boundary for private feed artifacts.

The caller must attach an exact immutable artifact first, capture the expected
verified profile snapshot, and perform the strict provider predecessor read
outside database locks.  This module performs no provider or object-storage
I/O.  A successful return means that the endpoint pointer and the durable
``SUBMIT_UNKNOWN`` boundary committed together; there is deliberately no
automatic pointer rollback after that boundary.

Production admits either the bounded operator canary or one exact account in
the active cutover allowlist. Keeping the checks local as well as in settings
prevents a direct call from widening either activation path.
"""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.feed_workflow import (
    FeedRunClaim,
    account_identity_digest,
    persist_feed_submission_boundary,
)
from apps.marketplaces.feed_cutover import private_feed_cutover_enabled
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)


_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
_BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
_MAX_REVISION = (1 << 63) - 1
_MAX_ARTIFACT_BYTES = 1_073_741_824
_MAX_ARTIFACT_LISTINGS = 10_000
_PROMOTION_ARTIFACT_MODES = frozenset({'canary', 'active'})


class FeedArtifactPromotionError(RuntimeError):
    """Base class for the private artifact promotion boundary."""


class FeedArtifactPromotionConfigurationError(FeedArtifactPromotionError):
    """The coordinated private/durable activation gates are not enabled."""


class StaleFeedArtifactPromotion(FeedArtifactPromotionError):
    """The exact claim, profile, artifact, or endpoint snapshot changed."""


def _require_activation_gates(account_id: int) -> tuple[str, str]:
    artifact_mode = getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', None)
    canary_ready = (
        artifact_mode == 'canary'
        and getattr(settings, 'MARKETPLACE_FEED_RUN_MODE', None) == 'durable'
        and getattr(settings, 'MARKETPLACE_FEED_INGRESS_MODE', None) == 'durable'
        and getattr(settings, 'MARKETPLACE_FEED_STORAGE_MODE', None)
        == 'private_generation'
    )
    active_ready = artifact_mode == 'active' and private_feed_cutover_enabled(
        account_id,
    )
    if not (canary_ready or active_ready):
        raise FeedArtifactPromotionConfigurationError(
            'Private feed artifact promotion is not activated.',
        )

    bucket = getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_BUCKET', None)
    if not isinstance(bucket, str) or _BUCKET_RE.fullmatch(bucket) is None:
        raise FeedArtifactPromotionConfigurationError(
            'The private feed artifact bucket is not safely configured.',
        )
    return bucket, str(artifact_mode)


def _profile_revision(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_REVISION
    ):
        raise ValueError('expected_profile_revision must be a positive revision.')
    return value


def _profile_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(
            'expected_profile_fingerprint must be a lowercase SHA-256 value.',
        )
    return value


def _artifact_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ValueError('artifact_id must be an exact UUID.')
    return value


def _transition_time(value: datetime | None) -> datetime:
    value = value or timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError('now must be a timezone-aware datetime.')
    return value


def _submission_time(value: datetime, *, now: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or timezone.is_naive(value)
        or value > now
    ):
        raise ValueError(
            'submitted_at must be a timezone-aware time no later than now.',
        )
    return value


def _valid_object_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and bool(value)
        and value.lower() != 'null'
        and len(value) <= 1024
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _configured_artifact_byte_cap() -> int:
    value = getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', None)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_ARTIFACT_BYTES
    ):
        raise FeedArtifactPromotionConfigurationError(
            'The private feed artifact byte cap is not safely configured.',
        )
    return value


def _claim_is_exact(
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
    *,
    now: datetime,
) -> bool:
    return (
        isinstance(claim, FeedRunClaim)
        and claim.state == MarketplaceFeedRun.State.PREPARING
        and claim.submitted_at is None
        and claim.provider_run_id is None
        and claim.provider_predecessor_run_id is None
        and run.pk == claim.run_id
        and run.account_id == claim.account_id
        and run.tenant_id == claim.tenant_id
        and run.marketplace == claim.marketplace
        and run.account_identity_digest == claim.account_identity_digest
        and run.payload_sha256 == claim.payload_sha256
        and run.state == MarketplaceFeedRun.State.PREPARING
        and run.revision == claim.revision
        and run.claim_token == claim.claim_token
        and run.claimed_until == claim.claimed_until
        and run.claimed_until is not None
        and run.claimed_until > now
        and run.submitted_at is None
        and run.provider_run_id is None
        and run.provider_predecessor_run_id is None
    )


def _expected_object_key(
    endpoint: MarketplaceFeedEndpoint,
    run: MarketplaceFeedRun,
    artifact: MarketplaceFeedArtifact,
) -> str:
    return (
        f'private-feeds/v1/{endpoint.pk}/{run.pk}/'
        f'{artifact.upload_attempt:05d}/feed.xml'
    )


def _validate_locked_snapshot(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
    upload_attempt: MarketplaceFeedArtifactUploadAttempt,
    artifact: MarketplaceFeedArtifact,
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
    expected_profile_revision: int,
    expected_profile_fingerprint: str,
    configured_bucket: str,
    artifact_mode: str,
    max_bytes: int,
    submitted_at: datetime,
    now: datetime,
) -> bool:
    tenant = account.tenant
    try:
        current_identity_digest = account_identity_digest(account)
    except Exception as exc:
        raise StaleFeedArtifactPromotion(
            'The marketplace account identity cannot be verified.',
        ) from exc

    source_revision = run.source_intent_revision
    endpoint_revision = run.endpoint_revision
    profile_verified_at = endpoint.profile_verified_at
    artifact_verified_at = artifact.verified_at
    attached_at = upload_attempt.attached_at
    resolved_at = upload_attempt.resolved_at
    if (
        isinstance(endpoint_revision, int)
        and not isinstance(endpoint_revision, bool)
        and 0 <= endpoint_revision < _MAX_REVISION
    ):
        valid_endpoint_revision = True
        expected_promoted_revision = endpoint_revision + 1
    else:
        valid_endpoint_revision = False
        expected_promoted_revision = -1
    initial_pointer = (
        valid_endpoint_revision
        and endpoint.current_artifact_id == run.predecessor_artifact_id
        and endpoint.artifact_revision == endpoint_revision
    )
    already_promoted = (
        valid_endpoint_revision
        and endpoint.current_artifact_id == artifact.pk
        and endpoint.artifact_revision == expected_promoted_revision
        and endpoint.artifact_promoted_at is not None
    )
    if artifact_mode == 'active':
        endpoint_transition_is_valid = (
            endpoint.serve_enabled is True
            and (
                (
                    initial_pointer
                    and endpoint.storage_mode
                    in {
                        MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
                        MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
                    }
                )
                or (
                    already_promoted
                    and endpoint.storage_mode
                    == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
                )
            )
        )
    else:
        endpoint_transition_is_valid = (
            initial_pointer
            and endpoint.storage_mode
            == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
            and endpoint.serve_enabled is False
        )
    if (
        account.deleted_at is not None
        or account.is_active is not True
        or tenant.is_active is not True
        or not _claim_is_exact(run, claim, now=now)
        or account.pk != claim.account_id
        or account.tenant_id != claim.tenant_id
        or account.marketplace != claim.marketplace
        or endpoint.account_id != account.pk
        or not hmac.compare_digest(current_identity_digest, claim.account_identity_digest)
        or not hmac.compare_digest(endpoint.owner_identity_digest, current_identity_digest)
        or not hmac.compare_digest(run.account_identity_digest, current_identity_digest)
        or not endpoint_transition_is_valid
        or endpoint.profile_state != MarketplaceFeedEndpoint.ProfileState.VERIFIED
        or endpoint.profile_revision != expected_profile_revision
        or not hmac.compare_digest(
            endpoint.profile_fingerprint,
            expected_profile_fingerprint,
        )
        or profile_verified_at is None
        or timezone.is_naive(profile_verified_at)
        or profile_verified_at > submitted_at
        or endpoint.previous_token_key_id != ''
        or source_revision is None
        or source_revision < 1
        or endpoint_revision is None
        or endpoint_revision < 0
        or endpoint_revision >= _MAX_REVISION
        or account.feed_intent_revision != source_revision
        or endpoint.source_intent_revision != source_revision
        or (
            endpoint.current_artifact_id is None
            and (
                endpoint.artifact_revision != 0
                or endpoint.artifact_promoted_at is not None
            )
        )
        or (
            endpoint.current_artifact_id is not None
            and endpoint.artifact_promoted_at is None
        )
        or run.feed_artifact_id != artifact.pk
        or artifact.pk == run.predecessor_artifact_id
        or upload_attempt.state
        != MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
        or upload_attempt.account_id != account.pk
        or upload_attempt.endpoint_id != endpoint.pk
        or upload_attempt.run_id != run.pk
        or upload_attempt.attempt_no != run.artifact_upload_attempt
        or upload_attempt.put_run_revision is None
        or upload_attempt.put_run_revision > run.revision
        or artifact.endpoint_id != endpoint.pk
        or artifact.account_id != account.pk
        or artifact.run_id != run.pk
        or artifact.upload_attempt != run.artifact_upload_attempt
        or not 1 <= artifact.upload_attempt <= 32767
        or artifact.payload_sha256 != run.payload_sha256
        or artifact.payload_sha256 != claim.payload_sha256
        or _DIGEST_RE.fullmatch(str(artifact.payload_sha256 or '')) is None
        or upload_attempt.storage_bucket != artifact.storage_bucket
        or upload_attempt.object_key != artifact.object_key
        or upload_attempt.object_version_id != artifact.object_version_id
        or upload_attempt.payload_sha256 != artifact.payload_sha256
        or upload_attempt.size_bytes != artifact.size_bytes
        or upload_attempt.projection_count != artifact.listing_count
        or upload_attempt.content_type != artifact.content_type
        or upload_attempt.verified_at != artifact.verified_at
        or upload_attempt.safe_error_code != ''
        or not 0 <= upload_attempt.projection_count <= _MAX_ARTIFACT_LISTINGS
        or not 1 <= artifact.size_bytes <= max_bytes
        or artifact.content_type != MarketplaceFeedArtifact.CONTENT_TYPE_XML
        or artifact.verification_method
        != MarketplaceFeedArtifact.VERIFICATION_VERSION_READBACK_SHA256
        or artifact.storage_bucket != configured_bucket
        or artifact.object_key != _expected_object_key(endpoint, run, artifact)
        or not _valid_object_version(artifact.object_version_id)
        or artifact_verified_at is None
        or timezone.is_naive(artifact_verified_at)
        or artifact_verified_at > submitted_at
        or attached_at is None
        or timezone.is_naive(attached_at)
        or attached_at > submitted_at
        or resolved_at is None
        or timezone.is_naive(resolved_at)
        or resolved_at > submitted_at
    ):
        raise StaleFeedArtifactPromotion(
            'The private feed promotion snapshot is stale or unsafe.',
        )
    return already_promoted


@transaction.atomic(durable=True)
def persist_private_feed_promotion_boundary(
    claim: FeedRunClaim,
    *,
    artifact_id: uuid.UUID,
    expected_profile_revision: int,
    expected_profile_fingerprint: str,
    provider_predecessor_run_id: str,
    submitted_at: datetime,
    now: datetime | None = None,
) -> FeedRunClaim:
    """Promote an attached artifact and persist the pre-POST boundary.

    The caller must have captured ``expected_profile_*`` before its strict
    provider predecessor read. Rows are locked in account -> endpoint -> run
    -> upload ledger -> artifact order. ``persist_feed_submission_boundary`` executes
    inside this outer transaction, so a stale/invalid boundary raises and
    rolls the endpoint pointer back.  After a successful return the pointer is
    one-way; later provider failures require reconciliation, never rollback.
    """

    configured_bucket, artifact_mode = _require_activation_gates(claim.account_id)
    max_bytes = _configured_artifact_byte_cap()
    artifact_id = _artifact_id(artifact_id)
    expected_profile_revision = _profile_revision(expected_profile_revision)
    expected_profile_fingerprint = _profile_fingerprint(
        expected_profile_fingerprint,
    )
    transition_at = _transition_time(now)
    submitted_at = _submission_time(submitted_at, now=transition_at)
    if not isinstance(claim, FeedRunClaim):
        raise StaleFeedArtifactPromotion('An exact FeedRunClaim is required.')

    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .filter(pk=claim.account_id)
        .first()
    )
    if account is None:
        raise StaleFeedArtifactPromotion('The marketplace account no longer exists.')
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .filter(account_id=account.pk)
        .first()
    )
    if endpoint is None:
        raise StaleFeedArtifactPromotion('The private feed endpoint no longer exists.')
    run = (
        MarketplaceFeedRun.objects.select_for_update(of=('self',))
        .filter(pk=claim.run_id, account_id=account.pk)
        .first()
    )
    if run is None:
        raise StaleFeedArtifactPromotion('The feed run no longer exists.')
    upload_attempt = (
        MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
            of=('self',),
        )
        .filter(
            run_id=run.pk,
            attempt_no=run.artifact_upload_attempt,
            state=MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
        )
        .first()
    )
    if upload_attempt is None:
        raise StaleFeedArtifactPromotion(
            'The attached feed upload ledger no longer matches.',
        )
    artifact = (
        MarketplaceFeedArtifact.objects.select_for_update(of=('self',))
        .filter(
            pk=artifact_id,
            account_id=account.pk,
            endpoint_id=endpoint.pk,
            run_id=run.pk,
            upload_attempt=upload_attempt.attempt_no,
        )
        .first()
    )
    if artifact is None:
        raise StaleFeedArtifactPromotion(
            'The attached feed artifact no longer matches.',
        )

    already_promoted = _validate_locked_snapshot(
        account=account,
        endpoint=endpoint,
        upload_attempt=upload_attempt,
        artifact=artifact,
        run=run,
        claim=claim,
        expected_profile_revision=expected_profile_revision,
        expected_profile_fingerprint=expected_profile_fingerprint,
        configured_bucket=configured_bucket,
        artifact_mode=artifact_mode,
        max_bytes=max_bytes,
        submitted_at=submitted_at,
        now=transition_at,
    )

    promoted_at = transition_at
    promoted_revision = endpoint.artifact_revision + 1
    source_intent_revision = run.source_intent_revision
    endpoint_revision = run.endpoint_revision
    if source_intent_revision is None or endpoint_revision is None:
        raise StaleFeedArtifactPromotion(
            'The feed run has no private source or endpoint revision.',
        )
    if not already_promoted:
        filters = {
            'pk': endpoint.pk,
            'account_id': account.pk,
            'owner_identity_digest': endpoint.owner_identity_digest,
            'storage_mode': endpoint.storage_mode,
            'serve_enabled': endpoint.serve_enabled,
            'profile_state': MarketplaceFeedEndpoint.ProfileState.VERIFIED,
            'profile_revision': expected_profile_revision,
            'profile_fingerprint': expected_profile_fingerprint,
            'source_intent_revision': source_intent_revision,
            'artifact_revision': endpoint_revision,
            'current_artifact_id': run.predecessor_artifact_id,
            'artifact_promoted_at': endpoint.artifact_promoted_at,
        }
        updates = {
            'current_artifact': artifact,
            'artifact_revision': promoted_revision,
            'source_intent_revision': source_intent_revision,
            'artifact_promoted_at': promoted_at,
            'updated_at': promoted_at,
        }
        if artifact_mode == 'active':
            updates.update(
                storage_mode=(
                    MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
                ),
                serve_enabled=True,
            )
        changed = MarketplaceFeedEndpoint.objects.filter(**filters).update(**updates)
        if changed != 1:
            raise StaleFeedArtifactPromotion(
                'The feed endpoint changed before artifact promotion.',
            )

    boundary = persist_feed_submission_boundary(
        claim,
        provider_predecessor_run_id=provider_predecessor_run_id,
        submitted_at=submitted_at,
        now=transition_at,
    )
    if boundary is None:
        raise StaleFeedArtifactPromotion(
            'The feed submission boundary rejected the promoted snapshot.',
        )
    return boundary
