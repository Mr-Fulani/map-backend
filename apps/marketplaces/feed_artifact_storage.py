"""Crash-safe private storage boundary for immutable feed artifacts.

Every object PUT is preceded by a durable upload-attempt boundary. A replay
never repeats a PUT whose outcome may be unknown: it resumes exact VersionId
verification, attaches an already verified version, returns an exactly
attached artifact, or fails closed for reconciliation.

This module has no default-storage or provider-adapter fallback. The caller
must inject a client scoped to the private, versioned artifact bucket together
with the exact bucket owner and byte cap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Mapping, Protocol, runtime_checkable

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone

from apps.marketplaces.feed_workflow import (
    FeedRunClaim,
    account_identity_digest,
)
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)


MAX_ARTIFACT_BYTES = 1_073_741_824
MAX_ARTIFACT_LISTINGS = 10_000
MAX_UPLOAD_ATTEMPTS = 32_767
READ_CHUNK_BYTES = 1024 * 1024
PRIVATE_ARTIFACT_MODES = frozenset({'shadow', 'canary', 'active'})
_BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
_SAFE_OWNER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,254}$')


class FeedArtifactStorageError(RuntimeError):
    """Base class for the private artifact storage boundary."""


class FeedArtifactConfigurationError(FeedArtifactStorageError):
    """The injected private storage contract is incomplete or unsafe."""


class FeedArtifactContentError(FeedArtifactStorageError):
    """The local payload is not an exact bounded disk-backed generation."""


class StaleFeedArtifactClaim(FeedArtifactStorageError):
    """The generation no longer owns the exact artifact snapshot."""


@dataclass(frozen=True, slots=True)
class FeedArtifactObjectLocator:
    """Redaction-safe exact object location for recovery or GC."""

    bucket: str
    object_key: str
    object_version_id: str | None


class FeedArtifactUploadOutcomeUnknown(FeedArtifactStorageError):
    """PUT may have crossed the storage boundary and must not be repeated."""

    def __init__(
        self,
        message: str,
        *,
        locator: FeedArtifactObjectLocator,
        upload_attempt_id: uuid.UUID | None = None,
    ):
        super().__init__(message)
        self.locator = locator
        self.upload_attempt_id = upload_attempt_id


class FeedArtifactVerificationError(FeedArtifactStorageError):
    """A known exact object version failed HEAD or content readback."""

    def __init__(
        self,
        message: str,
        *,
        locator: FeedArtifactObjectLocator,
        upload_attempt_id: uuid.UUID | None = None,
    ):
        super().__init__(message)
        self.locator = locator
        self.upload_attempt_id = upload_attempt_id


class FeedArtifactAttemptBlocked(FeedArtifactStorageError):
    """A terminal attempt requires reconciliation and cannot be retried here."""

    def __init__(
        self,
        message: str,
        *,
        locator: FeedArtifactObjectLocator,
        upload_attempt_id: uuid.UUID,
    ):
        super().__init__(message)
        self.locator = locator
        self.upload_attempt_id = upload_attempt_id


class FeedArtifactResumeRequired(FeedArtifactStorageError):
    """An exact known version is safe but needs a current claim to continue."""

    def __init__(
        self,
        message: str,
        *,
        locator: FeedArtifactObjectLocator,
        upload_attempt_id: uuid.UUID,
    ):
        super().__init__(message)
        self.locator = locator
        self.upload_attempt_id = upload_attempt_id


class OrphanedFeedArtifactUpload(FeedArtifactStorageError):
    """A known object was not attached to its feed run."""

    def __init__(
        self,
        message: str,
        *,
        locator: FeedArtifactObjectLocator,
        stale: bool,
        upload_attempt_id: uuid.UUID | None = None,
    ):
        super().__init__(message)
        self.locator = locator
        self.stale = stale
        self.upload_attempt_id = upload_attempt_id


@runtime_checkable
class PrivateVersionedObjectClient(Protocol):
    """Injected private client with an explicitly one-shot write boundary.

    ``put_object_once`` is not an alias for a stock SDK ``put_object``.  Its
    adapter must disable automatic write retries (for botocore this means
    ``total_max_attempts=1``) and attest that configuration through
    ``put_total_max_attempts``.  Exact-version HEAD/GET reads may be retried.
    """

    put_total_max_attempts: int

    def put_object_once(self, **kwargs: object) -> Mapping[str, object]: ...

    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class _LocalPayload:
    size_bytes: int
    payload_sha256: str
    projection_count: int


@dataclass(frozen=True, slots=True)
class _AttemptDecision:
    attempt_id: uuid.UUID
    action: str
    artifact: MarketplaceFeedArtifact | None = None
    locator: FeedArtifactObjectLocator | None = None


_ACTION_BEGIN_PUT = 'begin_put'
_ACTION_VERIFY = 'verify'
_ACTION_ATTACH = 'attach'
_ACTION_ATTACHED = 'attached'
_ACTION_ORPHANED = 'orphaned'


def _safe_bucket(value: object) -> str:
    if (
        not isinstance(value, str)
        or value.strip() != value
        or not _BUCKET_RE.fullmatch(value)
    ):
        raise FeedArtifactConfigurationError(
            'Private artifact bucket must be an explicit safe lowercase S3 bucket.',
        )
    return value


def _safe_bucket_owner(value: object) -> str:
    if (
        not isinstance(value, str)
        or value.strip() != value
        or not _SAFE_OWNER_RE.fullmatch(value)
    ):
        raise FeedArtifactConfigurationError(
            'Expected private artifact bucket owner must be explicit and safe.',
        )
    return value


def _safe_cap(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ARTIFACT_BYTES
    ):
        raise FeedArtifactConfigurationError(
            f'Private artifact byte cap must be between 1 and {MAX_ARTIFACT_BYTES}.',
        )
    return value


def _assert_one_shot_client(client: object) -> None:
    if not isinstance(client, PrivateVersionedObjectClient):
        raise FeedArtifactConfigurationError(
            'An explicit one-shot private versioned object client is required.',
        )
    put_attempts = client.put_total_max_attempts
    if (
        isinstance(put_attempts, bool)
        or not isinstance(put_attempts, int)
        or put_attempts != 1
    ):
        raise FeedArtifactConfigurationError(
            'Private artifact PUT must attest exactly one total SDK attempt.',
        )


def _projection_count(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_ARTIFACT_LISTINGS
    ):
        raise FeedArtifactContentError(
            f'projection_count must be between 0 and {MAX_ARTIFACT_LISTINGS}.',
        )
    return value


def _payload_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise StaleFeedArtifactClaim('The feed run has no exact payload SHA-256.')
    return value


def _object_version_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or value.strip() != value
        or not value
        or value.lower() == 'null'
        or len(value) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError('Storage did not return an exact non-null VersionId.')
    return value


def _artifact_upload_enabled() -> None:
    if (
        getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', None)
        not in PRIVATE_ARTIFACT_MODES
    ):
        raise FeedArtifactConfigurationError(
            'Private feed artifact upload requires an explicit artifact mode.',
        )


def _object_key(
    endpoint_id: uuid.UUID,
    run_id: uuid.UUID,
    attempt_no: int,
) -> str:
    return (
        f'private-feeds/v1/{endpoint_id}/{run_id}/'
        f'{attempt_no:05d}/feed.xml'
    )


def _locator(
    attempt: MarketplaceFeedArtifactUploadAttempt,
) -> FeedArtifactObjectLocator:
    return FeedArtifactObjectLocator(
        bucket=attempt.storage_bucket,
        object_key=attempt.object_key,
        object_version_id=attempt.object_version_id,
    )


def _claim_is_live(
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
    now: datetime,
) -> bool:
    return (
        isinstance(claim, FeedRunClaim)
        and claim.state == MarketplaceFeedRun.State.PREPARING
        and claim.submitted_at is None
        and claim.provider_run_id is None
        and claim.provider_predecessor_run_id is None
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


def _claim_matches_frozen_generation(
    *,
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
) -> bool:
    """Bind a caller to the immutable generation without requiring its lease.

    This deliberately accepts a run revision newer than the PUT revision.  A
    lease renewal increments the run revision, but must not make an exact
    VersionId returned by the already-started PUT impossible to journal.
    """

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
        and hmac.compare_digest(
            run.account_identity_digest,
            claim.account_identity_digest,
        )
        and hmac.compare_digest(run.payload_sha256, claim.payload_sha256)
        and run.revision >= claim.revision
    )


def _generation_snapshot_is_current(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
) -> bool:
    """Return whether the immutable object may still become this run's artifact.

    Claim token/deadline ownership is checked separately.  That distinction is
    important: an expired or renewed lease leaves VERSION_KNOWN resumable for
    its new owner, while a changed owner/source/endpoint makes the object a
    genuine orphan.
    """

    try:
        current_owner_digest = account_identity_digest(account)
    except Exception:
        return False
    tenant = account.tenant
    source_revision = run.source_intent_revision
    endpoint_revision = run.endpoint_revision
    return (
        _claim_matches_frozen_generation(run=run, claim=claim)
        and account.pk == claim.account_id
        and account.tenant_id == claim.tenant_id
        and account.marketplace == claim.marketplace
        and account.deleted_at is None
        and account.is_active is True
        and tenant.is_active is True
        and hmac.compare_digest(
            current_owner_digest,
            claim.account_identity_digest,
        )
        and endpoint.account_id == account.pk
        and hmac.compare_digest(
            endpoint.owner_identity_digest,
            claim.account_identity_digest,
        )
        and (
            (
                endpoint.storage_mode
                == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
                and endpoint.serve_enabled is False
            )
            or (
                getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', None)
                in {'shadow', 'canary'}
                and endpoint.storage_mode
                == MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
                and endpoint.serve_enabled is True
            )
        )
        and run.state == MarketplaceFeedRun.State.PREPARING
        and run.submitted_at is None
        and run.provider_run_id is None
        and run.provider_predecessor_run_id is None
        and run.feed_artifact_id is None
        and run.artifact_upload_attempt == 0
        and source_revision is not None
        and source_revision >= 1
        and endpoint_revision is not None
        and endpoint_revision >= 0
        and account.feed_intent_revision == source_revision
        and endpoint.source_intent_revision == source_revision
        and endpoint.artifact_revision == endpoint_revision
        and endpoint.current_artifact_id == run.predecessor_artifact_id
    )


def _validate_locked_generation(
    *,
    account: MarketplaceAccount,
    endpoint: MarketplaceFeedEndpoint,
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
    now: datetime,
) -> None:
    _payload_digest(run.payload_sha256)
    if not _generation_snapshot_is_current(
        account=account,
        endpoint=endpoint,
        run=run,
        claim=claim,
    ):
        raise StaleFeedArtifactClaim(
            'The feed artifact owner, source, endpoint, or run snapshot is stale.',
        )
    if not _claim_is_live(run, claim, now):
        raise StaleFeedArtifactClaim('The feed run claim or lease is stale.')


def _locked_generation_rows(
    claim: FeedRunClaim,
) -> tuple[MarketplaceAccount, MarketplaceFeedEndpoint, MarketplaceFeedRun]:
    if not isinstance(claim, FeedRunClaim):
        raise StaleFeedArtifactClaim('An exact FeedRunClaim is required.')
    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .select_related('tenant')
        .filter(pk=claim.account_id)
        .first()
    )
    if account is None:
        raise StaleFeedArtifactClaim('The feed artifact owner does not exist.')
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .filter(account_id=account.pk)
        .first()
    )
    if endpoint is None:
        raise StaleFeedArtifactClaim('The private feed endpoint does not exist.')
    run = (
        MarketplaceFeedRun.objects.select_for_update(of=('self',))
        .filter(pk=claim.run_id, account_id=account.pk)
        .first()
    )
    if run is None:
        raise StaleFeedArtifactClaim('The feed run does not exist.')
    return account, endpoint, run


def _disk_payload_size_and_digest(
    payload_file: BinaryIO,
    *,
    max_bytes: int,
) -> tuple[int, str, int]:
    seekable = getattr(payload_file, 'seekable', None)
    if not callable(seekable) or not seekable():
        raise FeedArtifactContentError('Feed payload must be a seekable binary file.')
    try:
        original_position = payload_file.tell()
        payload_file.flush()
        descriptor = payload_file.fileno()
        file_stat = os.fstat(descriptor)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise FeedArtifactContentError(
            'Feed payload must be backed by a regular disk file.',
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise FeedArtifactContentError(
            'Feed payload must be backed by a regular disk file.',
        )
    if file_stat.st_size < 1:
        raise FeedArtifactContentError('Feed payload must not be empty.')
    if file_stat.st_size > max_bytes:
        raise FeedArtifactContentError(
            'Feed payload exceeds the explicit private artifact byte cap.',
        )

    digest = hashlib.sha256()
    observed_size = 0
    try:
        payload_file.seek(0)
        while True:
            chunk = payload_file.read(READ_CHUNK_BYTES)
            if chunk == b'':
                break
            if not isinstance(chunk, bytes):
                raise FeedArtifactContentError(
                    'Feed payload file must be opened in binary mode.',
                )
            observed_size += len(chunk)
            if observed_size > max_bytes:
                raise FeedArtifactContentError(
                    'Feed payload exceeds the explicit private artifact byte cap.',
                )
            digest.update(chunk)
        if observed_size != file_stat.st_size:
            raise FeedArtifactContentError(
                'Feed payload changed while it was being checksummed.',
            )
        after_stat = os.fstat(descriptor)
        if (
            after_stat.st_dev != file_stat.st_dev
            or after_stat.st_ino != file_stat.st_ino
            or after_stat.st_size != file_stat.st_size
            or after_stat.st_mtime_ns != file_stat.st_mtime_ns
        ):
            raise FeedArtifactContentError(
                'Feed payload changed while it was being checksummed.',
            )
        payload_file.seek(0)
    except FeedArtifactContentError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FeedArtifactContentError(
            'Feed payload could not be read exactly.',
        ) from exc
    return observed_size, digest.hexdigest(), original_position


def _restore_position(payload_file: BinaryIO, position: int) -> None:
    try:
        payload_file.seek(position)
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _assert_unattached_run(run: MarketplaceFeedRun) -> None:
    if run.feed_artifact_id is not None or run.artifact_upload_attempt != 0:
        raise StaleFeedArtifactClaim(
            'The feed run artifact pointer is not in its initial state.',
        )


def _attempt_matches_payload(
    attempt: MarketplaceFeedArtifactUploadAttempt,
    *,
    endpoint: MarketplaceFeedEndpoint,
    run: MarketplaceFeedRun,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> bool:
    return (
        attempt.account_id == run.account_id
        and attempt.endpoint_id == endpoint.pk
        and attempt.run_id == run.pk
        and attempt.storage_bucket == bucket
        and attempt.expected_bucket_owner == expected_bucket_owner
        and attempt.object_key
        == _object_key(endpoint.pk, run.pk, attempt.attempt_no)
        and hmac.compare_digest(
            attempt.payload_sha256,
            local.payload_sha256,
        )
        and attempt.size_bytes == local.size_bytes
        and attempt.projection_count == local.projection_count
        and attempt.content_type == MarketplaceFeedArtifact.CONTENT_TYPE_XML
    )


def _assert_attempt_matches_payload(
    attempt: MarketplaceFeedArtifactUploadAttempt,
    *,
    endpoint: MarketplaceFeedEndpoint,
    run: MarketplaceFeedRun,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> None:
    if not _attempt_matches_payload(
        attempt,
        endpoint=endpoint,
        run=run,
        local=local,
        bucket=bucket,
        expected_bucket_owner=expected_bucket_owner,
    ):
        raise FeedArtifactContentError(
            'The upload-attempt snapshot differs from the exact feed projection.',
        )


def _has_version_resolution_source(
    attempt: MarketplaceFeedArtifactUploadAttempt,
) -> bool:
    return attempt.put_resolution_source in (
        MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE,
        MarketplaceFeedArtifactUploadAttempt.ResolutionSource.
        OPERATOR_RECONCILIATION,
    )


def _attached_artifact_locked(
    *,
    attempt: MarketplaceFeedArtifactUploadAttempt,
    endpoint: MarketplaceFeedEndpoint,
    run: MarketplaceFeedRun,
) -> MarketplaceFeedArtifact:
    # Artifact is deliberately last in the canonical lock order.
    artifact = (
        MarketplaceFeedArtifact.objects.select_for_update(of=('self',))
        .filter(run_id=run.pk, upload_attempt=attempt.attempt_no)
        .first()
    )
    if (
        artifact is None
        or attempt.state != MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
        or run.feed_artifact_id != artifact.pk
        or run.artifact_upload_attempt != attempt.attempt_no
        or artifact.account_id != attempt.account_id
        or artifact.endpoint_id != endpoint.pk
        or artifact.run_id != run.pk
        or artifact.storage_bucket != attempt.storage_bucket
        or artifact.object_key != attempt.object_key
        or artifact.object_version_id != attempt.object_version_id
        or artifact.payload_sha256 != attempt.payload_sha256
        or artifact.size_bytes != attempt.size_bytes
        or artifact.listing_count != attempt.projection_count
        or artifact.content_type != attempt.content_type
        or artifact.verification_method
        != MarketplaceFeedArtifact.VERIFICATION_VERSION_READBACK_SHA256
        or artifact.verified_at != attempt.verified_at
        or attempt.put_run_revision is None
        or attempt.put_run_revision > run.revision
        or not _has_version_resolution_source(attempt)
        or attempt.attached_at is None
        or attempt.resolved_at is None
        or attempt.safe_error_code != ''
    ):
        raise StaleFeedArtifactClaim(
            'The attached artifact, run, and upload ledger no longer match exactly.',
        )
    return artifact


def _prepare_or_resume_attempt(
    claim: FeedRunClaim,
    *,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> _AttemptDecision:
    with transaction.atomic(durable=True):
        account, endpoint, run = _locked_generation_rows(claim)
        latest = (
            MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
                of=('self',),
            )
            .filter(run_id=run.pk)
            .order_by('-attempt_no')
            .first()
        )
        if latest is not None:
            _assert_attempt_matches_payload(
                latest,
                endpoint=endpoint,
                run=run,
                local=local,
                bucket=bucket,
                expected_bucket_owner=expected_bucket_owner,
            )
            if latest.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED:
                if not _claim_matches_frozen_generation(run=run, claim=claim):
                    raise StaleFeedArtifactClaim(
                        'The caller does not match the attached feed generation.',
                    )
                artifact = _attached_artifact_locked(
                    attempt=latest,
                    endpoint=endpoint,
                    run=run,
                )
                return _AttemptDecision(
                    attempt_id=latest.pk,
                    action=_ACTION_ATTACHED,
                    artifact=artifact,
                )

        _validate_locked_generation(
            account=account,
            endpoint=endpoint,
            run=run,
            claim=claim,
            now=timezone.now(),
        )
        if not hmac.compare_digest(run.payload_sha256, local.payload_sha256):
            raise FeedArtifactContentError(
                'Disk payload SHA-256 does not match the exact feed run.',
            )

        if latest is not None:
            _assert_unattached_run(run)
            if latest.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING:
                raise FeedArtifactUploadOutcomeUnknown(
                    'The prior private artifact PUT outcome requires reconciliation; PUT was not repeated.',
                    locator=_locator(latest),
                    upload_attempt_id=latest.pk,
                )
            if latest.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN:
                if not _has_version_resolution_source(latest):
                    raise StaleFeedArtifactClaim(
                        'The known VersionId has no durable resolution source.',
                    )
                return _AttemptDecision(latest.pk, _ACTION_VERIFY)
            if latest.state == MarketplaceFeedArtifactUploadAttempt.State.VERIFIED:
                if not _has_version_resolution_source(latest):
                    raise StaleFeedArtifactClaim(
                        'The verified VersionId has no durable resolution source.',
                    )
                return _AttemptDecision(latest.pk, _ACTION_ATTACH)
            if latest.state in (
                MarketplaceFeedArtifactUploadAttempt.State.ORPHANED,
                MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
            ):
                raise FeedArtifactAttemptBlocked(
                    'The terminal private artifact attempt requires explicit reconciliation.',
                    locator=_locator(latest),
                    upload_attempt_id=latest.pk,
                )
            if latest.state == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT:
                if latest.attempt_no >= MAX_UPLOAD_ATTEMPTS:
                    raise FeedArtifactAttemptBlocked(
                        'The private artifact upload-attempt limit was reached.',
                        locator=_locator(latest),
                        upload_attempt_id=latest.pk,
                    )
                attempt_no = latest.attempt_no + 1
                latest = None
            elif latest.state == MarketplaceFeedArtifactUploadAttempt.State.PREPARED:
                return _AttemptDecision(latest.pk, _ACTION_BEGIN_PUT)
            else:
                raise FeedArtifactAttemptBlocked(
                    'The private artifact upload attempt has an unsupported state.',
                    locator=_locator(latest),
                    upload_attempt_id=latest.pk,
                )
        else:
            _assert_unattached_run(run)
            attempt_no = 1

        if latest is None:
            latest = MarketplaceFeedArtifactUploadAttempt.objects.create(
                account=account,
                endpoint=endpoint,
                run=run,
                attempt_no=attempt_no,
                storage_bucket=bucket,
                expected_bucket_owner=expected_bucket_owner,
                object_key=_object_key(endpoint.pk, run.pk, attempt_no),
                payload_sha256=local.payload_sha256,
                size_bytes=local.size_bytes,
                projection_count=local.projection_count,
                content_type=MarketplaceFeedArtifact.CONTENT_TYPE_XML,
            )

        # Return from this transaction with PREPARED durably committed.  The
        # separate transition below commits PUT_PENDING before the one-shot
        # client is allowed to cross the object-storage boundary.
        return _AttemptDecision(latest.pk, _ACTION_BEGIN_PUT)


def _begin_put_attempt(
    claim: FeedRunClaim,
    *,
    attempt_id: uuid.UUID,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> tuple[MarketplaceFeedArtifactUploadAttempt, dict[str, str]]:
    with transaction.atomic(durable=True):
        account, endpoint, run = _locked_generation_rows(claim)
        _validate_locked_generation(
            account=account,
            endpoint=endpoint,
            run=run,
            claim=claim,
            now=timezone.now(),
        )
        _assert_unattached_run(run)
        attempt = (
            MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
                of=('self',),
            )
            .filter(pk=attempt_id, run_id=run.pk)
            .first()
        )
        if attempt is None:
            raise StaleFeedArtifactClaim('The prepared upload attempt disappeared.')
        _assert_attempt_matches_payload(
            attempt,
            endpoint=endpoint,
            run=run,
            local=local,
            bucket=bucket,
            expected_bucket_owner=expected_bucket_owner,
        )
        put_started_at = timezone.now()
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.PREPARED,
            put_run_revision__isnull=True,
            object_version_id__isnull=True,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            revision=F('revision') + 1,
            put_run_revision=run.revision,
            put_started_at=put_started_at,
            updated_at=put_started_at,
        )
        if changed != 1:
            raise StaleFeedArtifactClaim(
                'The upload attempt changed before the PUT boundary committed.',
            )
        attempt.refresh_from_db()
        return attempt, _metadata(attempt=attempt, run=run)


def _record_version_known(
    claim: FeedRunClaim,
    *,
    attempt_id: uuid.UUID,
    version_id: str,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> MarketplaceFeedArtifactUploadAttempt:
    """Capture a returned VersionId even after the PUT owner's lease changed.

    This is a journal-only transition.  It binds the immutable attempt to the
    claim that started the one-shot PUT, but intentionally does not require the
    mutable account/source/lease snapshot to remain current.  Verification and
    attachment re-establish those fences later under the current live claim.
    """

    with transaction.atomic(durable=True):
        account, endpoint, run = _locked_generation_rows(claim)
        attempt = (
            MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
                of=('self',),
            )
            .filter(pk=attempt_id, run_id=run.pk)
            .first()
        )
        if (
            attempt is None
            or attempt.put_run_revision != claim.revision
            or attempt.put_run_revision is None
            or attempt.put_run_revision > run.revision
            or not _claim_matches_frozen_generation(run=run, claim=claim)
            or attempt.account_id != account.pk
            or attempt.endpoint_id != endpoint.pk
        ):
            raise StaleFeedArtifactClaim(
                'The one-shot PUT boundary no longer matches its exact claim.',
            )
        _assert_attempt_matches_payload(
            attempt,
            endpoint=endpoint,
            run=run,
            local=local,
            bucket=bucket,
            expected_bucket_owner=expected_bucket_owner,
        )
        if attempt.state in (
            MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
        ):
            if (
                not _has_version_resolution_source(attempt)
                or attempt.object_version_id != version_id
            ):
                raise StaleFeedArtifactClaim(
                    'The upload attempt has no matching durable VersionId source.',
                )
            return attempt
        if attempt.state != MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING:
            raise StaleFeedArtifactClaim(
                'The one-shot PUT attempt cannot capture a VersionId now.',
            )
        known_at = timezone.now()
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            object_version_id__isnull=True,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            revision=F('revision') + 1,
            object_version_id=version_id,
            put_resolution_source=(
                MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
            ),
            version_known_at=known_at,
            updated_at=known_at,
        )
        if changed != 1:
            raise StaleFeedArtifactClaim(
                'The upload attempt changed before VersionId was recorded.',
            )
        attempt.refresh_from_db()
        return attempt


def _record_verified_or_observe_progress(
    claim: FeedRunClaim,
    *,
    attempt_id: uuid.UUID,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> _AttemptDecision:
    with transaction.atomic(durable=True):
        account, endpoint, run = _locked_generation_rows(claim)
        attempt = (
            MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
                of=('self',),
            )
            .filter(pk=attempt_id, run_id=run.pk)
            .first()
        )
        if attempt is None:
            raise StaleFeedArtifactClaim('The exact upload attempt disappeared.')
        _assert_attempt_matches_payload(
            attempt,
            endpoint=endpoint,
            run=run,
            local=local,
            bucket=bucket,
            expected_bucket_owner=expected_bucket_owner,
        )
        if (
            not _claim_matches_frozen_generation(run=run, claim=claim)
            or attempt.put_run_revision is None
            or attempt.put_run_revision > run.revision
        ):
            raise StaleFeedArtifactClaim(
                'The verified read no longer belongs to the frozen generation.',
            )
        if attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED:
            artifact = _attached_artifact_locked(
                attempt=attempt,
                endpoint=endpoint,
                run=run,
            )
            return _AttemptDecision(
                attempt.pk,
                _ACTION_ATTACHED,
                artifact=artifact,
                locator=_locator(attempt),
            )
        if attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERIFIED:
            return _AttemptDecision(
                attempt.pk,
                _ACTION_ATTACH,
                locator=_locator(attempt),
            )
        if attempt.state != MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN:
            raise FeedArtifactAttemptBlocked(
                'The exact upload attempt cannot accept verification progress.',
                locator=_locator(attempt),
                upload_attempt_id=attempt.pk,
            )

        if not _generation_snapshot_is_current(
            account=account,
            endpoint=endpoint,
            run=run,
            claim=claim,
        ):
            resolved_at = timezone.now()
            changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
                pk=attempt.pk,
                revision=attempt.revision,
                state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            ).update(
                state=MarketplaceFeedArtifactUploadAttempt.State.ORPHANED,
                revision=F('revision') + 1,
                resolved_at=resolved_at,
                safe_error_code='stale_generation',
                updated_at=resolved_at,
            )
            if changed != 1:
                raise StaleFeedArtifactClaim(
                    'The upload attempt changed while resolving a stale version.',
                )
            return _AttemptDecision(
                attempt.pk,
                _ACTION_ORPHANED,
                locator=_locator(attempt),
            )
        if not _claim_is_live(run, claim, timezone.now()):
            # Lease loss alone is not an orphan: a renewed claim may safely
            # resume the exact VERSION_KNOWN readback without another PUT.
            raise StaleFeedArtifactClaim(
                'The verifier no longer owns the current feed run claim.',
            )

        verified_at = timezone.now()
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            object_version_id__isnull=False,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            revision=F('revision') + 1,
            verified_at=verified_at,
            updated_at=verified_at,
        )
        if changed != 1:
            raise StaleFeedArtifactClaim(
                'The upload attempt changed before verification was recorded.',
            )
        return _AttemptDecision(
            attempt.pk,
            _ACTION_ATTACH,
            locator=_locator(attempt),
        )


def _metadata(
    *,
    attempt: MarketplaceFeedArtifactUploadAttempt,
    run: MarketplaceFeedRun,
) -> dict[str, str]:
    return {
        'payload-sha256': attempt.payload_sha256,
        'size-bytes': str(attempt.size_bytes),
        'projection-count': str(attempt.projection_count),
        'content-type': attempt.content_type,
        'tenant-id': str(run.tenant_id),
        'account-id': str(run.account_id),
        'endpoint-id': str(attempt.endpoint_id),
        'run-id': str(run.pk),
        'run-revision': str(attempt.put_run_revision),
        'source-intent-revision': str(run.source_intent_revision),
        'endpoint-revision': str(run.endpoint_revision),
        'upload-attempt': str(attempt.attempt_no),
        'owner-identity-digest': run.account_identity_digest,
    }


def _verification_start(
    claim: FeedRunClaim,
    *,
    attempt_id: uuid.UUID,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> tuple[
    _AttemptDecision,
    MarketplaceFeedArtifactUploadAttempt | None,
    dict[str, str] | None,
]:
    with transaction.atomic(durable=True):
        account, endpoint, run = _locked_generation_rows(claim)
        attempt = (
            MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
                of=('self',),
            )
            .filter(pk=attempt_id, run_id=run.pk)
            .first()
        )
        if attempt is None:
            raise StaleFeedArtifactClaim('The exact upload attempt disappeared.')
        _assert_attempt_matches_payload(
            attempt,
            endpoint=endpoint,
            run=run,
            local=local,
            bucket=bucket,
            expected_bucket_owner=expected_bucket_owner,
        )
        if (
            not _claim_matches_frozen_generation(run=run, claim=claim)
            or attempt.put_run_revision is None
            or attempt.put_run_revision > run.revision
        ):
            raise StaleFeedArtifactClaim(
                'The exact upload attempt no longer matches the generation.',
            )
        if attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED:
            artifact = _attached_artifact_locked(
                attempt=attempt,
                endpoint=endpoint,
                run=run,
            )
            return (
                _AttemptDecision(
                    attempt.pk,
                    _ACTION_ATTACHED,
                    artifact=artifact,
                    locator=_locator(attempt),
                ),
                None,
                None,
            )
        if attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERIFIED:
            return (
                _AttemptDecision(
                    attempt.pk,
                    _ACTION_ATTACH,
                    locator=_locator(attempt),
                ),
                None,
                None,
            )
        if attempt.state != MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN:
            raise FeedArtifactAttemptBlocked(
                'The exact upload attempt is not resumable by verification.',
                locator=_locator(attempt),
                upload_attempt_id=attempt.pk,
            )
        if not _generation_snapshot_is_current(
            account=account,
            endpoint=endpoint,
            run=run,
            claim=claim,
        ):
            resolved_at = timezone.now()
            changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
                pk=attempt.pk,
                revision=attempt.revision,
                state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            ).update(
                state=MarketplaceFeedArtifactUploadAttempt.State.ORPHANED,
                revision=F('revision') + 1,
                resolved_at=resolved_at,
                safe_error_code='stale_generation',
                updated_at=resolved_at,
            )
            if changed != 1:
                raise StaleFeedArtifactClaim(
                    'The upload attempt changed while resolving a stale version.',
                )
            return (
                _AttemptDecision(
                    attempt.pk,
                    _ACTION_ORPHANED,
                    locator=_locator(attempt),
                ),
                None,
                None,
            )
        if not _claim_is_live(run, claim, timezone.now()):
            raise StaleFeedArtifactClaim(
                'The verifier no longer owns the current feed run claim.',
            )
        _assert_unattached_run(run)
        return (
            _AttemptDecision(
                attempt.pk,
                _ACTION_VERIFY,
                locator=_locator(attempt),
            ),
            attempt,
            _metadata(attempt=attempt, run=run),
        )


def _response_mapping(value: object, *, operation: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(
            f'Private storage {operation} response is not a mapping.',
        )
    return value


def _verify_response_headers(
    response: Mapping[str, object],
    *,
    version_id: str,
    checksum_sha256: str,
    size_bytes: int,
    metadata: Mapping[str, str],
) -> None:
    if response.get('DeleteMarker') is True:
        raise ValueError('Exact private artifact version is a delete marker.')
    if _object_version_id(response.get('VersionId')) != version_id:
        raise ValueError('Private storage returned a different object VersionId.')
    if response.get('ChecksumSHA256') != checksum_sha256:
        raise ValueError(
            'Private storage did not preserve the exact SHA-256 checksum.',
        )
    content_length = response.get('ContentLength')
    if (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length != size_bytes
    ):
        raise ValueError('Private storage returned a different object size.')
    if response.get('ContentType') != MarketplaceFeedArtifact.CONTENT_TYPE_XML:
        raise ValueError('Private storage returned a different content type.')
    actual_metadata = response.get('Metadata')
    if not isinstance(actual_metadata, Mapping):
        raise ValueError('Private storage returned no artifact metadata.')
    if any(actual_metadata.get(key) != value for key, value in metadata.items()):
        raise ValueError('Private storage returned different artifact metadata.')


def _readback_sha256(
    response: Mapping[str, object],
    *,
    size_bytes: int,
    max_bytes: int,
) -> str:
    body = response.get('Body')
    read = getattr(body, 'read', None)
    if not callable(read):
        raise ValueError('Private storage GET returned no readable body.')
    digest = hashlib.sha256()
    observed_size = 0
    try:
        while True:
            chunk = read(READ_CHUNK_BYTES)
            if chunk == b'':
                break
            if not isinstance(chunk, bytes):
                raise ValueError(
                    'Private storage GET returned a non-binary body.',
                )
            observed_size += len(chunk)
            if observed_size > size_bytes or observed_size > max_bytes:
                raise ValueError(
                    'Private storage GET exceeded the exact artifact size.',
                )
            digest.update(chunk)
    finally:
        close = getattr(body, 'close', None)
        if callable(close):
            close()
    if observed_size != size_bytes:
        raise ValueError('Private storage GET returned a truncated artifact.')
    return digest.hexdigest()


def _attach_verified_attempt(
    claim: FeedRunClaim,
    *,
    attempt_id: uuid.UUID,
    local: _LocalPayload,
    bucket: str,
    expected_bucket_owner: str,
) -> _AttemptDecision:
    with transaction.atomic(durable=True):
        account, endpoint, run = _locked_generation_rows(claim)
        attempt = (
            MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
                of=('self',),
            )
            .filter(pk=attempt_id, run_id=run.pk)
            .first()
        )
        if attempt is None:
            raise StaleFeedArtifactClaim(
                'The verified upload attempt no longer matches the feed run.',
            )
        _assert_attempt_matches_payload(
            attempt,
            endpoint=endpoint,
            run=run,
            local=local,
            bucket=bucket,
            expected_bucket_owner=expected_bucket_owner,
        )
        if (
            not _claim_matches_frozen_generation(run=run, claim=claim)
            or attempt.object_version_id is None
            or attempt.verified_at is None
            or attempt.put_run_revision is None
            or attempt.put_run_revision > run.revision
        ):
            raise StaleFeedArtifactClaim(
                'The verified upload attempt no longer matches the feed run.',
            )
        if attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED:
            artifact = _attached_artifact_locked(
                attempt=attempt,
                endpoint=endpoint,
                run=run,
            )
            return _AttemptDecision(
                attempt.pk,
                _ACTION_ATTACHED,
                artifact=artifact,
                locator=_locator(attempt),
            )
        if attempt.state != MarketplaceFeedArtifactUploadAttempt.State.VERIFIED:
            raise FeedArtifactAttemptBlocked(
                'The exact upload attempt is not ready for attachment.',
                locator=_locator(attempt),
                upload_attempt_id=attempt.pk,
            )
        if not _generation_snapshot_is_current(
            account=account,
            endpoint=endpoint,
            run=run,
            claim=claim,
        ):
            resolved_at = timezone.now()
            changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
                pk=attempt.pk,
                revision=attempt.revision,
                state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            ).update(
                state=MarketplaceFeedArtifactUploadAttempt.State.ORPHANED,
                revision=F('revision') + 1,
                resolved_at=resolved_at,
                safe_error_code='stale_generation',
                updated_at=resolved_at,
            )
            if changed != 1:
                raise StaleFeedArtifactClaim(
                    'The upload attempt changed while resolving stale attachment.',
                )
            return _AttemptDecision(
                attempt.pk,
                _ACTION_ORPHANED,
                locator=_locator(attempt),
            )
        if not _claim_is_live(run, claim, timezone.now()):
            raise StaleFeedArtifactClaim(
                'The attacher no longer owns the current feed run claim.',
            )
        _assert_unattached_run(run)

        # All three writes are one outer transaction. Deferred DB guards reject
        # any commit missing the run pointer or final ATTACHED ledger state.
        artifact = MarketplaceFeedArtifact.objects.create(
            endpoint=endpoint,
            account=account,
            run=run,
            upload_attempt=attempt.attempt_no,
            storage_bucket=attempt.storage_bucket,
            object_key=attempt.object_key,
            object_version_id=attempt.object_version_id,
            payload_sha256=attempt.payload_sha256,
            size_bytes=attempt.size_bytes,
            listing_count=attempt.projection_count,
            content_type=attempt.content_type,
            verification_method=(
                MarketplaceFeedArtifact.VERIFICATION_VERSION_READBACK_SHA256
            ),
            verified_at=attempt.verified_at,
        )
        attached_at = timezone.now()
        changed = MarketplaceFeedRun.objects.filter(
            pk=run.pk,
            revision=claim.revision,
            state=MarketplaceFeedRun.State.PREPARING,
            claim_token=claim.claim_token,
            claimed_until=claim.claimed_until,
            feed_artifact__isnull=True,
            artifact_upload_attempt=0,
        ).update(
            feed_artifact=artifact,
            artifact_upload_attempt=attempt.attempt_no,
            updated_at=attached_at,
        )
        if changed != 1:
            raise StaleFeedArtifactClaim(
                'The feed run changed before artifact attachment.',
            )
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
            revision=F('revision') + 1,
            attached_at=attached_at,
            resolved_at=attached_at,
            updated_at=attached_at,
        )
        if changed != 1:
            raise StaleFeedArtifactClaim(
                'The upload attempt changed before atomic attachment.',
            )
        return _AttemptDecision(
            attempt.pk,
            _ACTION_ATTACHED,
            artifact=artifact,
            locator=_locator(attempt),
        )


@dataclass(frozen=True, slots=True)
class PrivateFeedArtifactStorageService:
    """Upload, verify and attach one exact private feed projection."""

    client: PrivateVersionedObjectClient
    bucket: str
    expected_bucket_owner: str
    max_bytes: int

    def __post_init__(self) -> None:
        _assert_one_shot_client(self.client)
        object.__setattr__(self, 'bucket', _safe_bucket(self.bucket))
        object.__setattr__(
            self,
            'expected_bucket_owner',
            _safe_bucket_owner(self.expected_bucket_owner),
        )
        object.__setattr__(self, 'max_bytes', _safe_cap(self.max_bytes))

    def _verify_exact_version(
        self,
        claim: FeedRunClaim,
        *,
        attempt_id: uuid.UUID,
        local: _LocalPayload,
    ) -> _AttemptDecision:
        try:
            decision, attempt, metadata = _verification_start(
                claim,
                attempt_id=attempt_id,
                local=local,
                bucket=self.bucket,
                expected_bucket_owner=self.expected_bucket_owner,
            )
        except StaleFeedArtifactClaim as exc:
            ledger = MarketplaceFeedArtifactUploadAttempt.objects.filter(
                pk=attempt_id,
            ).first()
            if ledger is None:
                raise
            raise FeedArtifactResumeRequired(
                'The exact known version requires a renewed current claim.',
                locator=_locator(ledger),
                upload_attempt_id=ledger.pk,
            ) from exc
        if decision.action == _ACTION_ORPHANED:
            raise OrphanedFeedArtifactUpload(
                'The exact private artifact version is stale and was orphaned.',
                locator=(
                    decision.locator
                    or FeedArtifactObjectLocator(self.bucket, '', None)
                ),
                stale=True,
                upload_attempt_id=decision.attempt_id,
            )
        if decision.action != _ACTION_VERIFY:
            return decision
        if attempt is None or metadata is None:
            raise StaleFeedArtifactClaim(
                'The verification decision has no exact object snapshot.',
            )
        version_id = _object_version_id(attempt.object_version_id)
        checksum_sha256 = base64.b64encode(
            bytes.fromhex(attempt.payload_sha256),
        ).decode('ascii')
        request = {
            'Bucket': attempt.storage_bucket,
            'Key': attempt.object_key,
            'VersionId': version_id,
            'ChecksumMode': 'ENABLED',
            'ExpectedBucketOwner': attempt.expected_bucket_owner,
        }
        locator = _locator(attempt)
        try:
            head_response = _response_mapping(
                self.client.head_object(**request),
                operation='HEAD',
            )
            _verify_response_headers(
                head_response,
                version_id=version_id,
                checksum_sha256=checksum_sha256,
                size_bytes=attempt.size_bytes,
                metadata=metadata,
            )
            get_response = _response_mapping(
                self.client.get_object(**request),
                operation='GET',
            )
            _verify_response_headers(
                get_response,
                version_id=version_id,
                checksum_sha256=checksum_sha256,
                size_bytes=attempt.size_bytes,
                metadata=metadata,
            )
            readback_sha256 = _readback_sha256(
                get_response,
                size_bytes=attempt.size_bytes,
                max_bytes=self.max_bytes,
            )
            if not hmac.compare_digest(
                readback_sha256,
                attempt.payload_sha256,
            ):
                raise ValueError(
                    'Exact private artifact readback SHA-256 differs from the feed run.',
                )
        except Exception as exc:
            raise FeedArtifactVerificationError(
                'Exact private artifact version failed HEAD/readback verification.',
                locator=locator,
                upload_attempt_id=attempt.pk,
            ) from exc
        try:
            decision = _record_verified_or_observe_progress(
                claim,
                attempt_id=attempt.pk,
                local=local,
                bucket=self.bucket,
                expected_bucket_owner=self.expected_bucket_owner,
            )
        except StaleFeedArtifactClaim as exc:
            raise FeedArtifactResumeRequired(
                'The verified exact version requires a renewed current claim.',
                locator=locator,
                upload_attempt_id=attempt.pk,
            ) from exc
        if decision.action == _ACTION_ORPHANED:
            raise OrphanedFeedArtifactUpload(
                'The exact private artifact version became stale before attachment.',
                locator=decision.locator or locator,
                stale=True,
                upload_attempt_id=attempt.pk,
            )
        return decision

    def upload_and_attach(
        self,
        claim: FeedRunClaim,
        *,
        payload_file: BinaryIO,
        projection_count: int,
    ) -> MarketplaceFeedArtifact:
        """Persist one exact projection without replaying an uncertain PUT."""

        _artifact_upload_enabled()
        _assert_one_shot_client(self.client)
        projection_count = _projection_count(projection_count)
        size_bytes, local_sha256, original_position = (
            _disk_payload_size_and_digest(
                payload_file,
                max_bytes=self.max_bytes,
            )
        )
        local = _LocalPayload(
            size_bytes=size_bytes,
            payload_sha256=local_sha256,
            projection_count=projection_count,
        )
        try:
            decision = _prepare_or_resume_attempt(
                claim,
                local=local,
                bucket=self.bucket,
                expected_bucket_owner=self.expected_bucket_owner,
            )
            if decision.action == _ACTION_ATTACHED:
                if decision.artifact is None:
                    raise StaleFeedArtifactClaim(
                        'The attached upload attempt has no exact artifact.',
                    )
                return decision.artifact

            attempt_id = decision.attempt_id
            if decision.action == _ACTION_BEGIN_PUT:
                attempt, metadata = _begin_put_attempt(
                    claim,
                    attempt_id=attempt_id,
                    local=local,
                    bucket=self.bucket,
                    expected_bucket_owner=self.expected_bucket_owner,
                )
                checksum_sha256 = base64.b64encode(
                    bytes.fromhex(attempt.payload_sha256),
                ).decode('ascii')
                unresolved_locator = _locator(attempt)
                # At this exact point no write method has been invoked.  Keep a
                # changed adapter contract distinct from an unknown PUT result;
                # the conservative PUT_PENDING row still needs reconciliation.
                _assert_one_shot_client(self.client)
                try:
                    put_response = self.client.put_object_once(
                        Bucket=attempt.storage_bucket,
                        Key=attempt.object_key,
                        Body=payload_file,
                        ContentLength=attempt.size_bytes,
                        ContentType=attempt.content_type,
                        Metadata=metadata,
                        ChecksumSHA256=checksum_sha256,
                        ExpectedBucketOwner=attempt.expected_bucket_owner,
                    )
                except Exception as exc:
                    raise FeedArtifactUploadOutcomeUnknown(
                        'Private artifact PUT outcome is unknown; PUT must not be repeated.',
                        locator=unresolved_locator,
                        upload_attempt_id=attempt.pk,
                    ) from exc
                finally:
                    _restore_position(payload_file, original_position)

                try:
                    put_response = _response_mapping(
                        put_response,
                        operation='PUT',
                    )
                    version_id = _object_version_id(
                        put_response.get('VersionId'),
                    )
                except ValueError as exc:
                    raise FeedArtifactUploadOutcomeUnknown(
                        'Private artifact PUT returned no usable exact version; PUT must not be repeated.',
                        locator=unresolved_locator,
                        upload_attempt_id=attempt.pk,
                    ) from exc

                exact_locator = FeedArtifactObjectLocator(
                    bucket=attempt.storage_bucket,
                    object_key=attempt.object_key,
                    object_version_id=version_id,
                )
                try:
                    attempt = _record_version_known(
                        claim,
                        attempt_id=attempt.pk,
                        version_id=version_id,
                        local=local,
                        bucket=self.bucket,
                        expected_bucket_owner=self.expected_bucket_owner,
                    )
                except Exception as exc:
                    raise FeedArtifactUploadOutcomeUnknown(
                        'Exact VersionId could not be durably recorded; PUT must not be repeated.',
                        locator=exact_locator,
                        upload_attempt_id=attempt_id,
                    ) from exc
                if put_response.get('ChecksumSHA256') != checksum_sha256:
                    raise FeedArtifactUploadOutcomeUnknown(
                        'Private artifact PUT returned no exact SHA-256 checksum; '
                        'exact VersionId requires verification.',
                        locator=_locator(attempt),
                        upload_attempt_id=attempt.pk,
                    )
                decision = _AttemptDecision(attempt.pk, _ACTION_VERIFY)

            if decision.action == _ACTION_VERIFY:
                decision = self._verify_exact_version(
                    claim,
                    attempt_id=decision.attempt_id,
                    local=local,
                )

            if decision.action == _ACTION_ATTACHED:
                if decision.artifact is None:
                    raise StaleFeedArtifactClaim(
                        'The attached upload attempt has no exact artifact.',
                    )
                return decision.artifact

            if decision.action != _ACTION_ATTACH:
                raise StaleFeedArtifactClaim(
                    'The upload attempt produced no safe continuation.',
                )
            try:
                decision = _attach_verified_attempt(
                    claim,
                    attempt_id=decision.attempt_id,
                    local=local,
                    bucket=self.bucket,
                    expected_bucket_owner=self.expected_bucket_owner,
                )
            except StaleFeedArtifactClaim as exc:
                stored_attempt = MarketplaceFeedArtifactUploadAttempt.objects.filter(
                    pk=decision.attempt_id,
                ).first()
                if stored_attempt is None:
                    raise
                raise FeedArtifactResumeRequired(
                    'The verified exact version requires a renewed current claim.',
                    locator=_locator(stored_attempt),
                    upload_attempt_id=decision.attempt_id,
                ) from exc
            except DatabaseError as exc:
                stored_attempt = MarketplaceFeedArtifactUploadAttempt.objects.filter(
                    pk=decision.attempt_id,
                ).first()
                if stored_attempt is None:
                    raise
                raise OrphanedFeedArtifactUpload(
                    'Verified private artifact attachment did not commit and is retryable.',
                    locator=_locator(stored_attempt),
                    stale=False,
                    upload_attempt_id=decision.attempt_id,
                ) from exc
            if decision.action == _ACTION_ORPHANED:
                raise OrphanedFeedArtifactUpload(
                    'Verified private artifact became stale before database attachment.',
                    locator=(
                        decision.locator
                        or FeedArtifactObjectLocator(self.bucket, '', None)
                    ),
                    stale=True,
                    upload_attempt_id=decision.attempt_id,
                )
            if decision.action != _ACTION_ATTACHED or decision.artifact is None:
                raise StaleFeedArtifactClaim(
                    'The upload attempt produced no attached artifact.',
                )
            return decision.artifact
        finally:
            _restore_position(payload_file, original_position)
