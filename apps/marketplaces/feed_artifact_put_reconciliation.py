"""Dark reconciliation for an immutable artifact PUT with unknown outcome.

The write path invokes one non-retried PUT and durably leaves the upload
ledger in ``PUT_PENDING`` when that request has no trustworthy response.  A
second PUT is forbidden.  This module can only inspect the exact version
history after an operator has confirmed that the originating process ended
and a fixed settlement window has elapsed.

There is deliberately no client factory, task, command, or production wiring
here.  The injected adapter must attest that its exact-key
``ListObjectVersions`` absence is authoritative and strongly consistent for
the configured versioned bucket.  Even with that attestation, a zero-version
result is labelled as a reviewed settlement policy.  A real-bucket canary and
an accepted storage SLA remain activation gates.  Every applied decision is
atomically paired with a redacted, append-only operator audit; settlement,
transport, unexpected parser failures, and superseded no-ops create none.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, NoReturn, Protocol, cast

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedPutReconciliationAudit,
    MarketplaceFeedRun,
)


PUT_PENDING_SETTLEMENT_WINDOW_SECONDS = 15 * 60
PUT_PENDING_SETTLEMENT_WINDOW = timedelta(
    seconds=PUT_PENDING_SETTLEMENT_WINDOW_SECONDS,
)
LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 4
MAX_LIST_ENTRIES = LIST_PAGE_SIZE * MAX_LIST_PAGES

OUTCOME_NO_OBJECT = 'no_object_by_reviewed_settlement_policy'
OUTCOME_VERSION_KNOWN = 'version_known'
OUTCOME_MANUAL_REVIEW = 'manual_review'
OUTCOME_SUPERSEDED = 'superseded'

NO_OBJECT_AUDIT_CODE = 'reviewed_settlement_no_object'
MANUAL_DELETE_MARKER = 'put_reconcile_delete_marker'
MANUAL_MULTIPLE_VERSIONS = 'put_reconcile_multiple_versions'
MANUAL_UNUSABLE_VERSION = 'put_reconcile_unusable_version'
MANUAL_MALFORMED_LISTING = 'put_reconcile_malformed_listing'
MANUAL_PAGE_LIMIT = 'put_reconcile_page_limit'

_REFERENCE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
_REVISION_TOKEN_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')
_SAFE_KEY_RE = re.compile(r'^[\x21-\x7e]{1,255}$')
_MAX_REVISION = 9_223_372_036_854_775_807
_MAX_PROCESS_ID = 2_147_483_647


class PutPendingReconciliationError(RuntimeError):
    """Redaction-safe refusal before the upload ledger can be settled."""

    def __init__(self, code: str):
        self.code = code
        super().__init__('PUT-pending artifact reconciliation was refused.')


class _MalformedVersionListing(ValueError):
    """An explicit, reviewed response-shape violation safe to terminalize."""


@dataclass(frozen=True, slots=True)
class PutOriginTerminationAttestation:
    """Operator evidence that reconciliation runs outside the PUT process.

    ``origin_process_id`` is an operational fence, not a globally unique
    process identity.  The supervisor/operator must rule out PID reuse and
    bind ``evidence_reference`` to its incident record.  The reference and PID
    are input-only gates and are never persisted or returned.  Only keyed,
    lowercase SHA-256 identity/evidence digests and bounded revision tokens are
    written to the immutable reconciliation audit row.
    """

    evidence_reference: str = field(repr=False)
    evidence_digest: str = field(repr=False)
    operator_identity_digest: str = field(repr=False)
    origin_process_identity_digest: str = field(repr=False)
    digest_scheme_revision: str
    identity_digest_key_revision: str
    origin_process_id: int = field(repr=False)
    origin_process_terminated_at: datetime
    operator_confirmed: bool


@dataclass(frozen=True, slots=True)
class PutPendingAttemptReference:
    """Exact tenant-scoped optimistic reference reviewed by an operator."""

    tenant_id: int
    account_id: int
    endpoint_id: uuid.UUID
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    expected_revision: int


@dataclass(frozen=True, slots=True)
class PutPendingReconciliationResult:
    """Compact result that never contains bucket, key, or VersionId."""

    attempt_id: uuid.UUID
    outcome: str
    state: str
    revision: int
    applied: bool
    pages_scanned: int
    entries_scanned: int
    exact_version_count: int
    exact_delete_marker_count: int
    settlement_remaining_seconds: int = 0


class AuthoritativeExactVersionListClient(Protocol):
    """Injected adapter for authoritative exact-key version enumeration.

    A raw SDK client does not satisfy the safety contract.  A deployment
    adapter must explicitly attest the versioned bucket's strong,
    authoritative absence semantics after its real-bucket canary has passed.
    Read retries are safe; no write method is accepted or called here.
    """

    authoritative_exact_key_version_listing: bool
    adapter_policy_revision: str
    canary_policy_revision: str

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class _AttemptSnapshot:
    attempt_id: uuid.UUID
    revision: int
    storage_bucket: str
    expected_bucket_owner: str
    object_key: str
    size_bytes: int
    put_run_revision: int
    put_started_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedOperatorAuthorization:
    evidence_digest: str
    operator_identity_digest: str
    origin_process_identity_digest: str
    digest_scheme_revision: str
    identity_digest_key_revision: str
    origin_process_terminated_at: datetime


@dataclass(frozen=True, slots=True)
class _ValidatedClientPolicy:
    adapter_policy_revision: str
    canary_policy_revision: str


@dataclass(frozen=True, slots=True)
class _ListDecision:
    state: str
    safe_error_code: str
    version_id: str | None
    pages_scanned: int
    entries_scanned: int
    exact_version_count: int
    exact_delete_marker_count: int


def _refuse(code: str) -> NoReturn:
    raise PutPendingReconciliationError(code)


def _positive_integer(value: object, *, code: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        _refuse(code)
    return value


def _uuid(value: object, *, code: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        _refuse(code)
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        _refuse(code)


def _aware_datetime(value: object, *, code: str) -> datetime:
    if not isinstance(value, datetime) or timezone.is_naive(value):
        _refuse(code)
    return value


def _lowercase_hex_digest(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        _refuse(code)
    return value


def _revision_token(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _REVISION_TOKEN_RE.fullmatch(value):
        _refuse(code)
    return value


def _normalized_reference(
    reference: PutPendingAttemptReference,
) -> PutPendingAttemptReference:
    if type(reference) is not PutPendingAttemptReference:
        _refuse('invalid_attempt_reference')
    return PutPendingAttemptReference(
        tenant_id=_positive_integer(
            reference.tenant_id,
            code='invalid_tenant_id',
            maximum=_MAX_REVISION,
        ),
        account_id=_positive_integer(
            reference.account_id,
            code='invalid_account_id',
            maximum=_MAX_REVISION,
        ),
        endpoint_id=_uuid(
            reference.endpoint_id,
            code='invalid_endpoint_id',
        ),
        run_id=_uuid(reference.run_id, code='invalid_run_id'),
        attempt_id=_uuid(reference.attempt_id, code='invalid_attempt_id'),
        expected_revision=_positive_integer(
            reference.expected_revision,
            code='invalid_attempt_revision',
            maximum=_MAX_REVISION,
        ),
    )


def _validate_termination_attestation(
    attestation: PutOriginTerminationAttestation,
) -> _ValidatedOperatorAuthorization:
    if type(attestation) is not PutOriginTerminationAttestation:
        _refuse('termination_attestation_required')
    if (
        not isinstance(attestation.evidence_reference, str)
        or not _REFERENCE_RE.fullmatch(attestation.evidence_reference)
    ):
        _refuse('invalid_termination_evidence_reference')
    if attestation.operator_confirmed is not True:
        _refuse('origin_process_termination_unconfirmed')
    process_id = _positive_integer(
        attestation.origin_process_id,
        code='invalid_origin_process_id',
        maximum=_MAX_PROCESS_ID,
    )
    if process_id == os.getpid():
        _refuse('origin_process_is_current_process')
    return _ValidatedOperatorAuthorization(
        evidence_digest=_lowercase_hex_digest(
            attestation.evidence_digest,
            code='invalid_evidence_digest',
        ),
        operator_identity_digest=_lowercase_hex_digest(
            attestation.operator_identity_digest,
            code='invalid_operator_identity_digest',
        ),
        origin_process_identity_digest=_lowercase_hex_digest(
            attestation.origin_process_identity_digest,
            code='invalid_origin_process_identity_digest',
        ),
        digest_scheme_revision=_revision_token(
            attestation.digest_scheme_revision,
            code='invalid_digest_scheme_revision',
        ),
        identity_digest_key_revision=_revision_token(
            attestation.identity_digest_key_revision,
            code='invalid_identity_digest_key_revision',
        ),
        origin_process_terminated_at=_aware_datetime(
            attestation.origin_process_terminated_at,
            code='invalid_origin_process_termination_time',
        ),
    )


def _assert_client(
    client: object,
) -> tuple[AuthoritativeExactVersionListClient, _ValidatedClientPolicy]:
    typed_client = cast(AuthoritativeExactVersionListClient, client)
    try:
        authoritative_listing = typed_client.authoritative_exact_key_version_listing
        list_object_versions = typed_client.list_object_versions
        adapter_policy_revision = typed_client.adapter_policy_revision
        canary_policy_revision = typed_client.canary_policy_revision
    except Exception:
        _refuse('authoritative_exact_version_client_unreadable')
    if not callable(list_object_versions):
        _refuse('authoritative_exact_version_client_required')
    if authoritative_listing is not True:
        _refuse('authoritative_exact_version_listing_not_attested')
    policy = _ValidatedClientPolicy(
        adapter_policy_revision=_revision_token(
            adapter_policy_revision,
            code='invalid_adapter_policy_revision',
        ),
        canary_policy_revision=_revision_token(
            canary_policy_revision,
            code='invalid_canary_policy_revision',
        ),
    )
    return typed_client, policy


def _lock_scope(
    reference: PutPendingAttemptReference,
) -> MarketplaceFeedArtifactUploadAttempt:
    """Acquire only canonical account -> endpoint -> run -> attempt locks."""

    account = (
        MarketplaceAccount.all_objects.select_for_update(of=('self',))
        .filter(pk=reference.account_id, tenant_id=reference.tenant_id)
        .first()
    )
    if account is None:
        _refuse('attempt_scope_unavailable')
    endpoint = (
        MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
        .filter(pk=reference.endpoint_id, account_id=account.pk)
        .first()
    )
    if endpoint is None:
        _refuse('attempt_scope_unavailable')
    run = (
        MarketplaceFeedRun.objects.select_for_update(of=('self',))
        .filter(
            pk=reference.run_id,
            tenant_id=reference.tenant_id,
            account_id=account.pk,
        )
        .first()
    )
    if run is None:
        _refuse('attempt_scope_unavailable')
    attempt = (
        MarketplaceFeedArtifactUploadAttempt.objects.select_for_update(
            of=('self',),
        )
        .filter(
            pk=reference.attempt_id,
            account_id=account.pk,
            endpoint_id=endpoint.pk,
            run_id=run.pk,
        )
        .first()
    )
    if attempt is None:
        _refuse('attempt_scope_unavailable')
    return attempt


def _load_pending_snapshot(
    reference: PutPendingAttemptReference,
) -> _AttemptSnapshot:
    with transaction.atomic(durable=True):
        attempt = _lock_scope(reference)
        if (
            attempt.state
            != MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
            or attempt.revision != reference.expected_revision
        ):
            _refuse('attempt_snapshot_changed')
        if (
            attempt.put_started_at is None
            or attempt.put_run_revision is None
            or attempt.object_version_id is not None
            or attempt.put_resolution_source != ''
            or attempt.version_known_at is not None
            or attempt.verified_at is not None
            or attempt.attached_at is not None
            or attempt.resolved_at is not None
            or attempt.safe_error_code != ''
        ):
            _refuse('put_pending_snapshot_malformed')
        return _AttemptSnapshot(
            attempt_id=attempt.pk,
            revision=attempt.revision,
            storage_bucket=attempt.storage_bucket,
            expected_bucket_owner=attempt.expected_bucket_owner,
            object_key=attempt.object_key,
            size_bytes=attempt.size_bytes,
            put_run_revision=attempt.put_run_revision,
            put_started_at=attempt.put_started_at,
        )


def _settlement_remaining_seconds(
    snapshot: _AttemptSnapshot,
    *,
    origin_terminated_at: datetime,
    reconciliation_started_at: datetime,
) -> int:
    if origin_terminated_at < snapshot.put_started_at:
        _refuse('termination_precedes_put_boundary')
    if origin_terminated_at > reconciliation_started_at:
        _refuse('termination_time_is_in_future')
    settlement_due = max(
        snapshot.put_started_at,
        origin_terminated_at,
    ) + PUT_PENDING_SETTLEMENT_WINDOW
    return min(
        86_400,
        max(
            0,
            math.ceil(
                (settlement_due - reconciliation_started_at).total_seconds(),
            ),
        ),
    )


def _safe_listing_key(value: object, *, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_KEY_RE.fullmatch(value)
        or not value.startswith(prefix)
    ):
        raise _MalformedVersionListing('malformed listing key')
    return value


def _safe_marker(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value.strip() != value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise _MalformedVersionListing('malformed pagination marker')
    return value


def _usable_version_id(value: object) -> str:
    version_id = _safe_marker(value)
    if version_id.lower() == 'null':
        raise _MalformedVersionListing('unversioned object is not usable')
    return version_id


def _page_entries(
    response: object,
    *,
    snapshot: _AttemptSnapshot,
    requested_key_marker: str | None,
    requested_version_marker: str | None,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]], bool, tuple[str, str] | None]:
    if not isinstance(response, Mapping):
        raise _MalformedVersionListing(
            'version listing response is not a mapping',
        )
    if response.get('Name') != snapshot.storage_bucket:
        raise _MalformedVersionListing('version listing bucket changed')
    if response.get('Prefix') != snapshot.object_key:
        raise _MalformedVersionListing('version listing prefix changed')
    if response.get('MaxKeys') != LIST_PAGE_SIZE:
        raise _MalformedVersionListing('version listing page size changed')
    allowed_key_markers = (
        requested_key_marker,
        requested_key_marker or '',
    )
    if response.get('KeyMarker', requested_key_marker or '') not in (
        allowed_key_markers
    ):
        raise _MalformedVersionListing('version listing key marker changed')
    allowed_version_markers = (
        requested_version_marker,
        requested_version_marker or '',
    )
    if response.get(
        'VersionIdMarker',
        requested_version_marker or '',
    ) not in allowed_version_markers:
        raise _MalformedVersionListing(
            'version listing version marker changed',
        )
    versions = response.get('Versions', [])
    delete_markers = response.get('DeleteMarkers', [])
    common_prefixes = response.get('CommonPrefixes', [])
    if (
        not isinstance(versions, list)
        or not isinstance(delete_markers, list)
        or common_prefixes not in (None, [])
        or len(versions) + len(delete_markers) > LIST_PAGE_SIZE
        or any(not isinstance(entry, Mapping) for entry in versions)
        or any(not isinstance(entry, Mapping) for entry in delete_markers)
    ):
        raise _MalformedVersionListing('malformed version listing entries')
    truncated = response.get('IsTruncated')
    if not isinstance(truncated, bool):
        raise _MalformedVersionListing(
            'version listing truncation flag is missing',
        )
    next_markers: tuple[str, str] | None = None
    if truncated:
        if not versions and not delete_markers:
            raise _MalformedVersionListing(
                'truncated version listing page is empty',
            )
        next_markers = (
            _safe_listing_key(
                response.get('NextKeyMarker'),
                prefix=snapshot.object_key,
            ),
            _safe_marker(response.get('NextVersionIdMarker')),
        )
    elif (
        response.get('NextKeyMarker') not in (None, '')
        or response.get('NextVersionIdMarker') not in (None, '')
    ):
        raise _MalformedVersionListing(
            'terminal version listing has continuation markers',
        )
    return versions, delete_markers, truncated, next_markers


def _manual_decision(
    code: str,
    *,
    version_id: str | None = None,
    pages_scanned: int,
    entries_scanned: int,
    exact_version_count: int,
    exact_delete_marker_count: int,
) -> _ListDecision:
    return _ListDecision(
        state=MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
        safe_error_code=code,
        version_id=version_id,
        pages_scanned=pages_scanned,
        entries_scanned=entries_scanned,
        exact_version_count=exact_version_count,
        exact_delete_marker_count=exact_delete_marker_count,
    )


def _sole_known_version_id(version_ids: set[str]) -> str | None:
    if len(version_ids) != 1:
        return None
    return next(iter(version_ids))


def _list_exact_versions(
    client: AuthoritativeExactVersionListClient,
    snapshot: _AttemptSnapshot,
) -> _ListDecision:
    key_marker: str | None = None
    version_marker: str | None = None
    seen_markers: set[tuple[str, str]] = set()
    exact_version_ids: set[str] = set()
    exact_version_entries = 0
    single_exact_version_is_latest: bool | None = None
    exact_delete_markers = 0
    pages_scanned = 0
    entries_scanned = 0

    for page_number in range(1, MAX_LIST_PAGES + 1):
        kwargs: dict[str, object] = {
            'Bucket': snapshot.storage_bucket,
            'Prefix': snapshot.object_key,
            'ExpectedBucketOwner': snapshot.expected_bucket_owner,
            'MaxKeys': LIST_PAGE_SIZE,
        }
        if key_marker is not None:
            kwargs['KeyMarker'] = key_marker
            kwargs['VersionIdMarker'] = version_marker
        try:
            response = client.list_object_versions(**kwargs)
        except Exception:
            # No database mutation has occurred since the initial read-only
            # snapshot transaction committed.
            raise PutPendingReconciliationError(
                'version_listing_transport_failed',
            ) from None
        pages_scanned = page_number
        try:
            versions, delete_markers, truncated, next_markers = _page_entries(
                response,
                snapshot=snapshot,
                requested_key_marker=key_marker,
                requested_version_marker=version_marker,
            )
            entries_scanned += len(versions) + len(delete_markers)
            if entries_scanned > MAX_LIST_ENTRIES:
                raise _MalformedVersionListing(
                    'version listing entry limit exceeded',
                )
            unusable_exact_version = False
            for version in versions:
                listed_key = _safe_listing_key(
                    version.get('Key'),
                    prefix=snapshot.object_key,
                )
                if listed_key != snapshot.object_key:
                    continue
                exact_version_entries += 1
                try:
                    version_id = _usable_version_id(version.get('VersionId'))
                    exact_version_ids.add(version_id)
                except _MalformedVersionListing:
                    unusable_exact_version = True
                    continue
                try:
                    size = version.get('Size')
                    is_latest = version.get('IsLatest')
                    if (
                        isinstance(size, bool)
                        or not isinstance(size, int)
                        or size != snapshot.size_bytes
                        or not isinstance(is_latest, bool)
                    ):
                        raise _MalformedVersionListing(
                            'unusable exact version metadata',
                        )
                    if exact_version_entries == 1:
                        single_exact_version_is_latest = is_latest
                except _MalformedVersionListing:
                    unusable_exact_version = True
            for delete_marker in delete_markers:
                listed_key = _safe_listing_key(
                    delete_marker.get('Key'),
                    prefix=snapshot.object_key,
                )
                if listed_key == snapshot.object_key:
                    exact_delete_markers += 1
            if exact_version_entries > 1:
                return _manual_decision(
                    MANUAL_MULTIPLE_VERSIONS,
                    version_id=_sole_known_version_id(exact_version_ids),
                    pages_scanned=pages_scanned,
                    entries_scanned=entries_scanned,
                    exact_version_count=exact_version_entries,
                    exact_delete_marker_count=exact_delete_markers,
                )
            if exact_delete_markers:
                return _manual_decision(
                    MANUAL_DELETE_MARKER,
                    version_id=_sole_known_version_id(exact_version_ids),
                    pages_scanned=pages_scanned,
                    entries_scanned=entries_scanned,
                    exact_version_count=exact_version_entries,
                    exact_delete_marker_count=exact_delete_markers,
                )
            if unusable_exact_version:
                return _manual_decision(
                    MANUAL_UNUSABLE_VERSION,
                    version_id=_sole_known_version_id(exact_version_ids),
                    pages_scanned=pages_scanned,
                    entries_scanned=entries_scanned,
                    exact_version_count=exact_version_entries,
                    exact_delete_marker_count=exact_delete_markers,
                )
            if not truncated:
                if not exact_version_entries:
                    return _ListDecision(
                        state=(
                            MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
                        ),
                        safe_error_code=NO_OBJECT_AUDIT_CODE,
                        version_id=None,
                        pages_scanned=pages_scanned,
                        entries_scanned=entries_scanned,
                        exact_version_count=0,
                        exact_delete_marker_count=0,
                    )
                if single_exact_version_is_latest is not True:
                    return _manual_decision(
                        MANUAL_UNUSABLE_VERSION,
                        version_id=_sole_known_version_id(exact_version_ids),
                        pages_scanned=pages_scanned,
                        entries_scanned=entries_scanned,
                        exact_version_count=1,
                        exact_delete_marker_count=0,
                    )
                return _ListDecision(
                    state=(
                        MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
                    ),
                    safe_error_code='',
                    version_id=_sole_known_version_id(exact_version_ids),
                    pages_scanned=pages_scanned,
                    entries_scanned=entries_scanned,
                    exact_version_count=1,
                    exact_delete_marker_count=0,
                )
            if next_markers is None or next_markers in seen_markers:
                raise _MalformedVersionListing(
                    'version listing pagination did not advance',
                )
            seen_markers.add(next_markers)
            key_marker, version_marker = next_markers
        except _MalformedVersionListing:
            return _manual_decision(
                MANUAL_MALFORMED_LISTING,
                version_id=_sole_known_version_id(exact_version_ids),
                pages_scanned=pages_scanned,
                entries_scanned=entries_scanned,
                exact_version_count=exact_version_entries,
                exact_delete_marker_count=exact_delete_markers,
            )
        except Exception:
            # A code bug or an adapter Mapping runtime failure is not durable
            # evidence.  Leave PUT_PENDING untouched for a reviewed retry.
            raise PutPendingReconciliationError(
                'version_listing_parse_failed',
            ) from None

    return _manual_decision(
        MANUAL_PAGE_LIMIT,
        version_id=_sole_known_version_id(exact_version_ids),
        pages_scanned=pages_scanned,
        entries_scanned=entries_scanned,
        exact_version_count=exact_version_entries,
        exact_delete_marker_count=exact_delete_markers,
    )


def _result(
    *,
    attempt_id: uuid.UUID,
    outcome: str,
    state: str,
    revision: int,
    applied: bool,
    decision: _ListDecision | None = None,
    settlement_remaining_seconds: int = 0,
) -> PutPendingReconciliationResult:
    return PutPendingReconciliationResult(
        attempt_id=attempt_id,
        outcome=outcome,
        state=state,
        revision=revision,
        applied=applied,
        pages_scanned=decision.pages_scanned if decision else 0,
        entries_scanned=decision.entries_scanned if decision else 0,
        exact_version_count=decision.exact_version_count if decision else 0,
        exact_delete_marker_count=(
            decision.exact_delete_marker_count if decision else 0
        ),
        settlement_remaining_seconds=settlement_remaining_seconds,
    )


def _apply_decision(
    reference: PutPendingAttemptReference,
    snapshot: _AttemptSnapshot,
    decision: _ListDecision,
    *,
    authorization: _ValidatedOperatorAuthorization,
    client_policy: _ValidatedClientPolicy,
    reconciliation_started_at: datetime,
) -> PutPendingReconciliationResult:
    resolved_at = timezone.now()
    update_values: dict[str, object] = {
        'state': decision.state,
        'revision': F('revision') + 1,
        'put_resolution_source': (
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.OPERATOR_RECONCILIATION
        ),
        'updated_at': resolved_at,
    }
    if decision.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN:
        update_values.update(
            object_version_id=decision.version_id,
            version_known_at=resolved_at,
        )
        outcome = OUTCOME_VERSION_KNOWN
    else:
        update_values.update(
            resolved_at=resolved_at,
            safe_error_code=decision.safe_error_code,
        )
        if decision.version_id is not None:
            update_values.update(
                object_version_id=decision.version_id,
                version_known_at=resolved_at,
            )
        outcome = (
            OUTCOME_NO_OBJECT
            if decision.state
            == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
            else OUTCOME_MANUAL_REVIEW
        )

    with transaction.atomic(durable=True):
        attempt = _lock_scope(reference)
        if (
            attempt.state
            != MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
            or attempt.revision != snapshot.revision
            or attempt.revision != reference.expected_revision
        ):
            return _result(
                attempt_id=attempt.pk,
                outcome=OUTCOME_SUPERSEDED,
                state=attempt.state,
                revision=attempt.revision,
                applied=False,
                decision=decision,
            )
        if (
            attempt.storage_bucket != snapshot.storage_bucket
            or attempt.expected_bucket_owner != snapshot.expected_bucket_owner
            or attempt.object_key != snapshot.object_key
            or attempt.size_bytes != snapshot.size_bytes
            or attempt.put_run_revision != snapshot.put_run_revision
            or attempt.put_started_at != snapshot.put_started_at
            or attempt.object_version_id is not None
            or attempt.put_resolution_source != ''
            or attempt.version_known_at is not None
            or attempt.verified_at is not None
            or attempt.attached_at is not None
            or attempt.resolved_at is not None
            or attempt.safe_error_code != ''
        ):
            _refuse('attempt_snapshot_changed')
        MarketplaceFeedPutReconciliationAudit.objects.create(
            attempt=attempt,
            pre_revision=snapshot.revision,
            post_revision=snapshot.revision + 1,
            from_state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            to_state=decision.state,
            outcome=outcome,
            decision_code=decision.safe_error_code,
            version_id_captured=decision.version_id is not None,
            origin_process_identity_digest=(
                authorization.origin_process_identity_digest
            ),
            operator_identity_digest=authorization.operator_identity_digest,
            evidence_digest=authorization.evidence_digest,
            digest_scheme_revision=authorization.digest_scheme_revision,
            identity_digest_key_revision=(
                authorization.identity_digest_key_revision
            ),
            adapter_policy_revision=client_policy.adapter_policy_revision,
            canary_policy_revision=client_policy.canary_policy_revision,
            origin_process_terminated_at=(
                authorization.origin_process_terminated_at
            ),
            reconciliation_started_at=reconciliation_started_at,
            decision_at=resolved_at,
            settlement_window_seconds=PUT_PENDING_SETTLEMENT_WINDOW_SECONDS,
            pages_scanned=decision.pages_scanned,
            entries_scanned=decision.entries_scanned,
            exact_version_count=decision.exact_version_count,
            exact_delete_marker_count=decision.exact_delete_marker_count,
        )
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=snapshot.revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            storage_bucket=snapshot.storage_bucket,
            expected_bucket_owner=snapshot.expected_bucket_owner,
            object_key=snapshot.object_key,
            size_bytes=snapshot.size_bytes,
            put_run_revision=snapshot.put_run_revision,
            put_started_at=snapshot.put_started_at,
            object_version_id__isnull=True,
            put_resolution_source='',
            version_known_at__isnull=True,
            verified_at__isnull=True,
            attached_at__isnull=True,
            resolved_at__isnull=True,
            safe_error_code='',
        ).update(**update_values)
        if changed != 1:
            # The canonical row lock makes a concurrent update impossible.
            # Treat any remaining CAS anomaly as a failed durable boundary so
            # the audit insert above rolls back with the attempted transition.
            _refuse('attempt_resolution_cas_failed')
        attempt.refresh_from_db(fields=('state', 'revision'))
        return _result(
            attempt_id=attempt.pk,
            outcome=outcome,
            state=attempt.state,
            revision=attempt.revision,
            applied=True,
            decision=decision,
        )


def reconcile_put_pending_upload_attempt(
    reference: PutPendingAttemptReference,
    *,
    client: AuthoritativeExactVersionListClient,
    termination: PutOriginTerminationAttestation,
) -> PutPendingReconciliationResult:
    """Settle one exact PUT_PENDING row without writing or deleting objects.

    The fixed start timestamp prevents time spent waiting for database locks
    from satisfying the settlement interval.  The two short database phases
    commit on either side of all object-storage I/O.
    """

    if connection.in_atomic_block:
        _refuse('reconciliation_inside_database_transaction')
    reconciliation_started_at = timezone.now()
    reference = _normalized_reference(reference)
    authorization = _validate_termination_attestation(termination)
    client, client_policy = _assert_client(client)
    snapshot = _load_pending_snapshot(reference)
    remaining = _settlement_remaining_seconds(
        snapshot,
        origin_terminated_at=authorization.origin_process_terminated_at,
        reconciliation_started_at=reconciliation_started_at,
    )
    if remaining:
        return _result(
            attempt_id=snapshot.attempt_id,
            outcome='settlement_pending',
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            revision=snapshot.revision,
            applied=False,
            settlement_remaining_seconds=remaining,
        )
    decision = _list_exact_versions(client, snapshot)
    _, observed_client_policy = _assert_client(client)
    if observed_client_policy != client_policy:
        _refuse('version_listing_policy_changed')
    return _apply_decision(
        reference,
        snapshot,
        decision,
        authorization=authorization,
        client_policy=client_policy,
        reconciliation_started_at=reconciliation_started_at,
    )
