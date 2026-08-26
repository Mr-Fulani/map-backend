"""Explicit, account-scoped P6 private-feed canary.

This module is deliberately not connected to Celery, the feed scheduler, or
normal tenant writes.  An operator builds and verifies one immutable artifact
while the existing legacy endpoint keeps serving, then atomically switches the
same stable URL to that exact version.  A separately fenced rollback restores
legacy serving without deleting either the database evidence or the object.
"""

from __future__ import annotations

import hashlib
import hmac
import tempfile
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO, cast

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.adapters.avito.feed_builder import (
    FeedWriteResult,
    build_stop_feed,
    write_feed,
)
from apps.marketplaces.feed_artifact_clients import (
    private_feed_bucket_preflight,
    private_feed_object_client,
)
from apps.marketplaces.feed_artifact_storage import PrivateFeedArtifactStorageService
from apps.marketplaces.feed_workflow import (
    FeedRunClaim,
    account_identity_digest,
    claim_due_run_for_account,
    finish_feed_run,
)
from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedPutReconciliationAudit,
    MarketplaceFeedRun,
)


_MAX_LISTINGS = 10_000
_SERVABLE_PROFILE_STATES = frozenset({
    MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    MarketplaceFeedEndpoint.ProfileState.MIGRATING,
    MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    MarketplaceFeedEndpoint.ProfileState.VERIFIED,
})


class PrivateFeedCanaryError(RuntimeError):
    """The exact one-account P6 canary cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class PrivateFeedCanaryInspection:
    account_id: int
    endpoint_id: uuid.UUID
    source_intent_revision: int
    dispatched_intent_revision: int
    artifact_revision: int
    listing_count: int
    endpoint_storage_mode: str
    profile_state: str
    serve_enabled: bool
    runtime_ready: bool


@dataclass(frozen=True, slots=True)
class PrivateFeedCanaryResult:
    account_id: int
    endpoint_id: uuid.UUID
    run_id: uuid.UUID
    artifact_id: uuid.UUID
    source_intent_revision: int
    artifact_revision: int
    listing_count: int
    size_bytes: int
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class PrivateFeedCanaryRollback:
    account_id: int
    endpoint_id: uuid.UUID
    artifact_id: uuid.UUID
    artifact_revision: int
    storage_mode: str


def _runtime_ready() -> bool:
    return (
        getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', None) == 'canary'
        and getattr(settings, 'MARKETPLACE_FEED_STORAGE_MODE', None)
        == 'private_generation'
        and getattr(settings, 'MARKETPLACE_FEED_RUN_MODE', None) == 'legacy'
        and getattr(settings, 'MARKETPLACE_FEED_INGRESS_MODE', None) == 'dual_write'
        and getattr(settings, 'AVITO_STATUS_LIFECYCLE_MODE', None) == 'dual_write'
        and getattr(settings, 'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED', None)
        is False
    )


def _require_runtime() -> None:
    if not _runtime_ready():
        raise PrivateFeedCanaryError(
            'P6 canary requires canary/private_generation with legacy run, '
            'dual_write ingress/lifecycle, and profile migration disabled.',
        )


def _positive_account_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('account_id must be a positive integer.')
    return value


def _artifact_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('expected_artifact_revision must be a positive integer.')
    return value


def _positive_revision(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{field_name} must be a positive integer.')
    return value


def _artifact_id(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ValueError('expected_artifact_id must be a UUID.') from None


def _required_uuid(value: object, *, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f'{field_name} must be a UUID.') from None


def _projection_statuses() -> tuple[str, ...]:
    statuses = [Listing.STATUS_ACTIVE, Listing.STATUS_PENDING]
    if getattr(settings, 'MARKETPLACE_FEED_INGRESS_MODE', 'legacy') == 'legacy':
        statuses.append(Listing.STATUS_QUEUED)
    return tuple(statuses)


def _projection(account_id: int):
    return (
        Listing.objects.filter(
            account_id=account_id,
            status__in=_projection_statuses(),
        )
        .select_related('tenant', 'product', 'account')
        .order_by('created_at', 'pk')
    )


def _live_account(account: MarketplaceAccount) -> bool:
    return (
        account.deleted_at is None
        and account.is_active is True
        and account.tenant.is_active is True
        and account.marketplace == MarketplaceAccount.MARKETPLACE_AVITO
    )


def _validate_legacy_snapshot(
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
) -> None:
    try:
        owner_digest = account_identity_digest(account)
    except Exception as exc:
        raise PrivateFeedCanaryError(
            'The marketplace account identity cannot be verified.',
        ) from exc
    if (
        not _live_account(account)
        or endpoint.account_id != account.pk
        or endpoint.storage_mode
        != MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
        or endpoint.serve_enabled is not True
        or endpoint.profile_state not in _SERVABLE_PROFILE_STATES
        or not endpoint.legacy_object_key
        or account.feed_intent_revision < 1
        or endpoint.source_intent_revision != account.feed_intent_revision
        or not hmac.compare_digest(endpoint.owner_identity_digest, owner_digest)
    ):
        raise PrivateFeedCanaryError(
            'The account does not have one exact live legacy stable endpoint.',
        )


def inspect_private_feed_canary(account_id: int) -> PrivateFeedCanaryInspection:
    """Return a redaction-safe, read-only readiness snapshot for one account."""

    account_id = _positive_account_id(account_id)
    account = (
        MarketplaceAccount.all_objects.select_related('tenant')
        .filter(pk=account_id)
        .first()
    )
    endpoint = (
        MarketplaceFeedEndpoint.objects.filter(account_id=account_id).first()
    )
    if account is None or endpoint is None:
        raise PrivateFeedCanaryError('The account or stable endpoint does not exist.')
    _validate_legacy_snapshot(account, endpoint)
    listing_count = _projection(account_id).count()
    if listing_count > _MAX_LISTINGS:
        raise PrivateFeedCanaryError(
            f'The private canary projection exceeds {_MAX_LISTINGS} listings.',
        )
    return PrivateFeedCanaryInspection(
        account_id=account.pk,
        endpoint_id=endpoint.pk,
        source_intent_revision=account.feed_intent_revision,
        dispatched_intent_revision=account.feed_intent_dispatched_revision,
        artifact_revision=endpoint.artifact_revision,
        listing_count=listing_count,
        endpoint_storage_mode=endpoint.storage_mode,
        profile_state=endpoint.profile_state,
        serve_enabled=endpoint.serve_enabled,
        runtime_ready=_runtime_ready(),
    )


def _write_projection(
    account_id: int,
    payload_file,
    *,
    listing_count: int,
    max_bytes: int,
) -> FeedWriteResult:
    if listing_count == 0:
        payload = build_stop_feed()
        if len(payload) > max_bytes:
            raise PrivateFeedCanaryError(
                'The Avito STOP feed exceeds the configured artifact byte cap.',
            )
        written = payload_file.write(payload)
        if written != len(payload):
            raise OSError('The canary payload file did not accept all STOP bytes.')
        payload_file.flush()
        payload_file.seek(0)
        return FeedWriteResult(
            listing_count=0,
            size_bytes=len(payload),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
        )
    result = write_feed(
        _projection(account_id).iterator(chunk_size=500),
        payload_file,
        max_bytes=max_bytes,
    )
    payload_file.flush()
    payload_file.seek(0)
    if result.listing_count != listing_count:
        raise PrivateFeedCanaryError(
            'The feed projection changed while the private artifact was built.',
        )
    return result


@transaction.atomic(durable=True)
def _create_canary_run(
    inspection: PrivateFeedCanaryInspection,
    payload: FeedWriteResult,
) -> MarketplaceFeedRun:
    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .get(pk=inspection.account_id)
    )
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .get(pk=inspection.endpoint_id, account_id=account.pk)
    )
    _validate_legacy_snapshot(account, endpoint)
    if (
        account.feed_intent_revision != inspection.source_intent_revision
        or endpoint.artifact_revision != inspection.artifact_revision
        or account.feed_intent_revision != account.feed_intent_dispatched_revision
        or account.feed_intent_due_at is not None
    ):
        raise PrivateFeedCanaryError(
            'The account feed changed or still has undispatched legacy work.',
        )
    if MarketplaceFeedRun.objects.filter(
        account_id=account.pk,
        state__in=MarketplaceFeedRun.OWNERSHIP_STATES,
    ).exists():
        raise PrivateFeedCanaryError(
            'Another feed generation still owns this account.',
        )
    if MarketplaceFeedRun.objects.filter(
        account_id=account.pk,
        source_intent_revision=account.feed_intent_revision,
    ).exists():
        raise PrivateFeedCanaryError(
            'This exact source revision already has a feed generation.',
        )
    return MarketplaceFeedRun.objects.create(
        tenant_id=account.tenant_id,
        account_id=account.pk,
        marketplace=account.marketplace,
        account_identity_digest=endpoint.owner_identity_digest,
        payload_sha256=payload.payload_sha256,
        state=MarketplaceFeedRun.State.PREPARING,
        next_attempt_at=timezone.now(),
        total_count=payload.listing_count,
        pending_count=0,
        source_intent_revision=account.feed_intent_revision,
        endpoint_revision=endpoint.artifact_revision,
        predecessor_artifact_id=endpoint.current_artifact_id,
    )


@transaction.atomic(durable=True)
def _validate_resumable_canary(
    inspection: PrivateFeedCanaryInspection,
    payload: FeedWriteResult,
    *,
    expected_run_id: uuid.UUID,
    expected_run_revision: int,
    expected_attempt_id: uuid.UUID,
    expected_attempt_revision: int,
) -> MarketplaceFeedRun:
    """Fence one exact NO_OBJECT or VERSION_KNOWN canary continuation."""

    checked_at = timezone.now()
    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .get(pk=inspection.account_id)
    )
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .get(pk=inspection.endpoint_id, account_id=account.pk)
    )
    run = (
        MarketplaceFeedRun.objects.select_for_update(of=('self',))
        .filter(pk=expected_run_id, account_id=account.pk)
        .first()
    )
    attempt = (
        MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
            of=('self',),
        )
        .filter(
            pk=expected_attempt_id,
            account_id=account.pk,
            endpoint_id=endpoint.pk,
            run_id=expected_run_id,
        )
        .first()
    )
    _validate_legacy_snapshot(account, endpoint)
    if run is None or attempt is None:
        raise PrivateFeedCanaryError(
            'The exact reconciled canary generation is unavailable.',
        )
    audit = MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt_id=attempt.pk,
    ).first()
    latest_attempt_id = (
        MarketplaceFeedArtifactUploadAttempt.objects.filter(run_id=run.pk)
        .order_by('-attempt_no')
        .values_list('pk', flat=True)
        .first()
    )
    active_claim = (
        run.claim_token is not None
        and run.claimed_until is not None
        and run.claimed_until > checked_at
    )
    shared_snapshot_changed = (
        run.state != MarketplaceFeedRun.State.PREPARING
        or run.revision != expected_run_revision
        or run.next_attempt_at is None
        or run.next_attempt_at > checked_at
        or active_claim
        or run.feed_artifact_id is not None
        or run.artifact_upload_attempt != 0
        or run.source_intent_revision != inspection.source_intent_revision
        or run.source_intent_revision != account.feed_intent_revision
        or account.feed_intent_dispatched_revision != run.source_intent_revision
        or account.feed_intent_due_at is not None
        or run.endpoint_revision != inspection.artifact_revision
        or run.endpoint_revision != endpoint.artifact_revision
        or run.predecessor_artifact_id != endpoint.current_artifact_id
        or run.total_count != payload.listing_count
        or run.pending_count != 0
        or not hmac.compare_digest(run.payload_sha256, payload.payload_sha256)
        or latest_attempt_id != attempt.pk
        or attempt.revision != expected_attempt_revision
        or attempt.storage_bucket
        != str(settings.MARKETPLACE_FEED_ARTIFACT_BUCKET)
        or attempt.expected_bucket_owner
        != str(settings.MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER)
        or attempt.payload_sha256 != payload.payload_sha256
        or attempt.size_bytes != payload.size_bytes
        or attempt.projection_count != payload.listing_count
        or attempt.content_type != MarketplaceFeedArtifact.CONTENT_TYPE_XML
    )
    no_object_resume = (
        attempt.state == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
        and attempt.put_resolution_source
        == (
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.
            OPERATOR_RECONCILIATION
        )
        and attempt.resolved_at is not None
        and attempt.safe_error_code == 'reviewed_settlement_no_object'
        and attempt.object_version_id is None
        and audit is not None
        and audit.pre_revision + 1 == attempt.revision
        and audit.post_revision == attempt.revision
        and audit.to_state
        == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
        and audit.outcome
        == (
            MarketplaceFeedPutReconciliationAudit.Outcome.
            NO_OBJECT_BY_REVIEWED_SETTLEMENT_POLICY
        )
        and audit.decision_code == 'reviewed_settlement_no_object'
        and audit.version_id_captured is False
    )
    version_known_resume = (
        attempt.state
        == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
        and attempt.put_resolution_source
        == MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
        and attempt.resolved_at is None
        and attempt.safe_error_code == ''
        and attempt.object_version_id is not None
        and attempt.object_version_id != ''
        and attempt.version_known_at is not None
        and attempt.put_run_revision is not None
        and attempt.put_run_revision <= run.revision
        and audit is None
    )
    if shared_snapshot_changed or not (no_object_resume or version_known_resume):
        raise PrivateFeedCanaryError(
            'The exact resumable canary snapshot changed before resume.',
        )
    return run


def _claim_upload_activate(
    inspection: PrivateFeedCanaryInspection,
    run: MarketplaceFeedRun,
    payload_file: BinaryIO,
    payload: FeedWriteResult,
    *,
    max_bytes: int,
) -> tuple[
    MarketplaceFeedArtifact,
    MarketplaceFeedRun,
    MarketplaceFeedEndpoint,
]:
    claim = claim_due_run_for_account(
        inspection.account_id,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        lease=timedelta(minutes=30),
    )
    if claim is None:
        raise PrivateFeedCanaryError('The exact P6 canary run could not be claimed.')
    service = PrivateFeedArtifactStorageService(
        private_feed_object_client(),
        bucket=str(settings.MARKETPLACE_FEED_ARTIFACT_BUCKET),
        expected_bucket_owner=str(
            settings.MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER,
        ),
        max_bytes=max_bytes,
    )
    artifact = service.upload_and_attach(
        claim,
        payload_file=payload_file,
        projection_count=payload.listing_count,
    )
    run, endpoint = _activate_attached_canary(claim, artifact.pk)
    return artifact, run, endpoint


def _result_from_activation(
    inspection: PrivateFeedCanaryInspection,
    artifact: MarketplaceFeedArtifact,
    run: MarketplaceFeedRun,
    endpoint: MarketplaceFeedEndpoint,
) -> PrivateFeedCanaryResult:
    source_intent_revision = run.source_intent_revision
    if source_intent_revision is None:
        raise PrivateFeedCanaryError('The activated canary lost its source revision.')
    return PrivateFeedCanaryResult(
        account_id=inspection.account_id,
        endpoint_id=endpoint.pk,
        run_id=run.pk,
        artifact_id=artifact.pk,
        source_intent_revision=source_intent_revision,
        artifact_revision=endpoint.artifact_revision,
        listing_count=artifact.listing_count,
        size_bytes=artifact.size_bytes,
        payload_sha256=artifact.payload_sha256,
    )


@transaction.atomic(durable=True)
def _activate_attached_canary(
    claim: FeedRunClaim,
    artifact_id: uuid.UUID,
) -> tuple[MarketplaceFeedRun, MarketplaceFeedEndpoint]:
    _require_runtime()
    transition_at = timezone.now()
    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .get(pk=claim.account_id)
    )
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .get(account_id=account.pk)
    )
    run = (
        MarketplaceFeedRun.objects.select_for_update(of=('self',))
        .get(pk=claim.run_id, account_id=account.pk)
    )
    artifact = (
        MarketplaceFeedArtifact.objects.select_for_update(of=('self',))
        .get(pk=artifact_id, run_id=run.pk)
    )
    attempt = (
        MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(of=('self',))
        .filter(
            run_id=run.pk,
            attempt_no=artifact.upload_attempt,
            state=MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
        )
        .first()
    )
    try:
        live_digest = account_identity_digest(account)
    except Exception as exc:
        raise PrivateFeedCanaryError(
            'The marketplace account identity cannot be verified.',
        ) from exc
    if (
        attempt is None
        or not _live_account(account)
        or run.state != MarketplaceFeedRun.State.PREPARING
        or run.revision != claim.revision
        or run.claim_token != claim.claim_token
        or run.claimed_until != claim.claimed_until
        or run.claimed_until is None
        or run.claimed_until <= transition_at
        or run.feed_artifact_id != artifact.pk
        or run.artifact_upload_attempt != artifact.upload_attempt
        or run.source_intent_revision != account.feed_intent_revision
        or endpoint.source_intent_revision != run.source_intent_revision
        or endpoint.artifact_revision != run.endpoint_revision
        or endpoint.current_artifact_id != run.predecessor_artifact_id
        or endpoint.storage_mode
        != MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
        or endpoint.serve_enabled is not True
        or endpoint.profile_state not in _SERVABLE_PROFILE_STATES
        or not endpoint.legacy_object_key
        or not hmac.compare_digest(endpoint.owner_identity_digest, live_digest)
        or not hmac.compare_digest(run.account_identity_digest, live_digest)
        or not hmac.compare_digest(run.payload_sha256, artifact.payload_sha256)
    ):
        raise PrivateFeedCanaryError(
            'The attached artifact no longer matches the exact canary snapshot.',
        )

    finished = finish_feed_run(
        claim,
        state=MarketplaceFeedRun.State.SUCCEEDED,
        now=transition_at,
    )
    promoted_revision = endpoint.artifact_revision + 1
    changed = MarketplaceFeedEndpoint.objects.filter(
        pk=endpoint.pk,
        account_id=account.pk,
        owner_identity_digest=endpoint.owner_identity_digest,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        serve_enabled=True,
        profile_state=endpoint.profile_state,
        profile_revision=endpoint.profile_revision,
        source_intent_revision=run.source_intent_revision,
        artifact_revision=run.endpoint_revision,
        current_artifact_id=run.predecessor_artifact_id,
        artifact_promoted_at=endpoint.artifact_promoted_at,
    ).update(
        current_artifact=artifact,
        artifact_revision=promoted_revision,
        artifact_promoted_at=transition_at,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        updated_at=transition_at,
    )
    if changed != 1:
        raise PrivateFeedCanaryError(
            'The stable endpoint changed before private canary activation.',
        )
    run.refresh_from_db()
    endpoint.refresh_from_db()
    if run.revision != finished.revision:
        raise PrivateFeedCanaryError('The canary run terminal revision changed.')
    return run, endpoint


def activate_private_feed_canary(account_id: int) -> PrivateFeedCanaryResult:
    """Build, upload, verify and atomically activate one reviewed account."""

    _require_runtime()
    private_feed_bucket_preflight()
    inspection = inspect_private_feed_canary(account_id)
    max_bytes = getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', None)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise PrivateFeedCanaryError('The private artifact byte cap is invalid.')

    with tempfile.NamedTemporaryFile(mode='w+b') as payload_file:
        payload = _write_projection(
            inspection.account_id,
            payload_file,
            listing_count=inspection.listing_count,
            max_bytes=max_bytes,
        )
        run = _create_canary_run(inspection, payload)
        artifact, run, endpoint = _claim_upload_activate(
            inspection,
            run,
            cast(BinaryIO, payload_file),
            payload,
            max_bytes=max_bytes,
        )
    return _result_from_activation(inspection, artifact, run, endpoint)


def resume_private_feed_canary(
    account_id: int,
    *,
    expected_run_id: uuid.UUID | str,
    expected_run_revision: int,
    expected_attempt_id: uuid.UUID | str,
    expected_attempt_revision: int,
) -> PrivateFeedCanaryResult:
    """Resume one audited NO_OBJECT canary using immutable attempt N+1."""

    _require_runtime()
    private_feed_bucket_preflight()
    expected_run_id = _required_uuid(
        expected_run_id,
        field_name='expected_run_id',
    )
    expected_attempt_id = _required_uuid(
        expected_attempt_id,
        field_name='expected_attempt_id',
    )
    expected_run_revision = _positive_revision(
        expected_run_revision,
        field_name='expected_run_revision',
    )
    expected_attempt_revision = _positive_revision(
        expected_attempt_revision,
        field_name='expected_attempt_revision',
    )
    inspection = inspect_private_feed_canary(account_id)
    max_bytes = getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', None)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise PrivateFeedCanaryError('The private artifact byte cap is invalid.')

    with tempfile.NamedTemporaryFile(mode='w+b') as payload_file:
        payload = _write_projection(
            inspection.account_id,
            payload_file,
            listing_count=inspection.listing_count,
            max_bytes=max_bytes,
        )
        run = _validate_resumable_canary(
            inspection,
            payload,
            expected_run_id=expected_run_id,
            expected_run_revision=expected_run_revision,
            expected_attempt_id=expected_attempt_id,
            expected_attempt_revision=expected_attempt_revision,
        )
        artifact, run, endpoint = _claim_upload_activate(
            inspection,
            run,
            cast(BinaryIO, payload_file),
            payload,
            max_bytes=max_bytes,
        )
    return _result_from_activation(inspection, artifact, run, endpoint)


@transaction.atomic(durable=True)
def rollback_private_feed_canary(
    account_id: int,
    *,
    expected_artifact_id: uuid.UUID | str,
    expected_artifact_revision: int,
) -> PrivateFeedCanaryRollback:
    """Restore legacy serving for one exact canary without deleting evidence."""

    account_id = _positive_account_id(account_id)
    expected_artifact_id = _artifact_id(expected_artifact_id)
    expected_artifact_revision = _artifact_revision(expected_artifact_revision)
    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .get(pk=account_id)
    )
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .get(account_id=account.pk)
    )
    if (
        endpoint.storage_mode
        != MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
        or endpoint.serve_enabled is not True
        or endpoint.current_artifact_id != expected_artifact_id
        or endpoint.artifact_revision != expected_artifact_revision
        or not endpoint.legacy_object_key
    ):
        raise PrivateFeedCanaryError(
            'The endpoint no longer matches the exact private canary rollback fence.',
        )
    changed = MarketplaceFeedEndpoint.objects.filter(
        pk=endpoint.pk,
        account_id=account.pk,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=True,
        current_artifact_id=expected_artifact_id,
        artifact_revision=expected_artifact_revision,
        artifact_promoted_at=endpoint.artifact_promoted_at,
    ).update(
        storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        updated_at=timezone.now(),
    )
    if changed != 1:
        raise PrivateFeedCanaryError('The private canary changed before rollback.')
    endpoint.refresh_from_db()
    return PrivateFeedCanaryRollback(
        account_id=account.pk,
        endpoint_id=endpoint.pk,
        artifact_id=expected_artifact_id,
        artifact_revision=endpoint.artifact_revision,
        storage_mode=endpoint.storage_mode,
    )
