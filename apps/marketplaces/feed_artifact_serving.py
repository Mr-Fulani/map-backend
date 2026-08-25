"""Fail-closed exact-version redirects for private marketplace feed artifacts.

Serving is available only to an explicitly promoted ``private_generation``
endpoint with ``serve_enabled`` and artifact mode ``canary`` or ``active``.
P6 production settings admit only the bounded canary; broad ``active`` mode
remains rejected.
"""

from __future__ import annotations

import hmac
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn
from urllib.parse import parse_qs, unquote, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.feed_endpoint import (
    accepted_marketplace_feed_capability_key_id,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedEndpoint,
    MarketplaceFeedFetchEvidence,
    MarketplaceFeedRun,
)


_ARTIFACT_BUCKET_RE = re.compile(
    r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$',
)
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
_KEY_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')
_SERVABLE_PROFILE_STATES = {
    MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    MarketplaceFeedEndpoint.ProfileState.MIGRATING,
    MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    MarketplaceFeedEndpoint.ProfileState.VERIFIED,
}
_S3_ENDPOINT = 'https://storage.yandexcloud.net'


ExactVersionPresigner = Callable[..., str]


class PrivateFeedNotServable(ValueError):
    """The private artifact snapshot cannot safely produce a redirect."""


@dataclass(frozen=True, slots=True)
class PrivateFeedRedirect:
    """A short-lived exact-version location created after durable evidence."""

    location: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _LockedPrivateFeedSnapshot:
    account: MarketplaceAccount
    endpoint: MarketplaceFeedEndpoint
    artifact: MarketplaceFeedArtifact
    run: MarketplaceFeedRun


def private_feed_route_enabled(endpoint: MarketplaceFeedEndpoint) -> bool:
    """Return whether one explicitly promoted endpoint may use private serving."""

    return (
        str(getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_MODE', 'disabled'))
        in {'canary', 'active'}
        and endpoint.storage_mode
        == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
        and endpoint.serve_enabled is True
    )


def _reject() -> NoReturn:
    raise PrivateFeedNotServable('Private marketplace feed is not servable.')


def _lock_private_feed_snapshot(
    public_id: uuid.UUID,
) -> _LockedPrivateFeedSnapshot:
    """Acquire the fetch trigger's account -> endpoint -> artifact -> run locks."""

    account_id = (
        MarketplaceFeedEndpoint.objects
        .filter(pk=public_id)
        .values_list('account_id', flat=True)
        .first()
    )
    if account_id is None:
        _reject()

    account = (
        MarketplaceAccount.all_objects
        .select_related('tenant')
        .select_for_update(of=('self',))
        .filter(pk=account_id)
        .first()
    )
    if account is None:
        _reject()

    endpoint = (
        MarketplaceFeedEndpoint.objects
        .select_related('account')
        .select_for_update(of=('self',))
        .filter(pk=public_id, account_id=account.pk)
        .first()
    )
    if endpoint is None or endpoint.current_artifact_id is None:
        _reject()

    artifact = (
        MarketplaceFeedArtifact.objects
        .select_for_update(of=('self',))
        .filter(pk=endpoint.current_artifact_id)
        .first()
    )
    if artifact is None:
        _reject()

    run = (
        MarketplaceFeedRun.objects
        .select_for_update(of=('self',))
        .filter(pk=artifact.run_id)
        .first()
    )
    if run is None:
        _reject()
    return _LockedPrivateFeedSnapshot(
        account=account,
        endpoint=endpoint,
        artifact=artifact,
        run=run,
    )


def _expected_object_key(snapshot: _LockedPrivateFeedSnapshot) -> str:
    return (
        f'private-feeds/v1/{snapshot.endpoint.pk}/{snapshot.run.pk}/'
        f'{snapshot.artifact.upload_attempt:05d}/feed.xml'
    )


def _configured_redirect_ttl() -> int:
    value = getattr(settings, 'MARKETPLACE_FEED_REDIRECT_TTL_SECONDS', 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 300:
        _reject()
    return value


def _configured_artifact_bucket() -> str:
    bucket = str(
        getattr(settings, 'MARKETPLACE_FEED_ARTIFACT_BUCKET', '') or '',
    )
    if not _ARTIFACT_BUCKET_RE.fullmatch(bucket):
        _reject()
    return bucket


def _validate_locked_private_snapshot(
    snapshot: _LockedPrivateFeedSnapshot,
    *,
    provided_capability: object,
) -> str:
    """Fence every owner, pointer, revision, and immutable object coordinate."""

    account = snapshot.account
    endpoint = snapshot.endpoint
    artifact = snapshot.artifact
    run = snapshot.run
    tenant = account.tenant

    if (
        not private_feed_route_enabled(endpoint)
        or endpoint.profile_state not in _SERVABLE_PROFILE_STATES
        or account.deleted_at is not None
        or account.is_active is not True
        or tenant.is_active is not True
        or endpoint.account_id != account.pk
        or artifact.endpoint_id != endpoint.pk
        or artifact.account_id != account.pk
        or endpoint.current_artifact_id != artifact.pk
        or endpoint.artifact_promoted_at is None
        or run.pk != artifact.run_id
        or run.account_id != account.pk
        or run.tenant_id != tenant.pk
        or run.marketplace != account.marketplace
        or run.feed_artifact_id != artifact.pk
        or endpoint.source_intent_revision != account.feed_intent_revision
        or run.source_intent_revision is None
        or run.endpoint_revision is None
        or run.endpoint_revision + 1 != endpoint.artifact_revision
        or run.artifact_upload_attempt != artifact.upload_attempt
        or run.predecessor_artifact_id == artifact.pk
        or run.payload_sha256 != artifact.payload_sha256
        or not _DIGEST_RE.fullmatch(str(artifact.payload_sha256 or ''))
        or artifact.content_type != MarketplaceFeedArtifact.CONTENT_TYPE_XML
        or artifact.verification_method
        != MarketplaceFeedArtifact.VERIFICATION_VERSION_READBACK_SHA256
        or artifact.object_key != _expected_object_key(snapshot)
        or artifact.storage_bucket != _configured_artifact_bucket()
    ):
        _reject()

    version_id = str(artifact.object_version_id or '')
    if (
        version_id != version_id.strip()
        or not version_id
        or version_id.lower() == 'null'
        or any(ord(character) < 32 or ord(character) == 127 for character in version_id)
    ):
        _reject()

    try:
        live_owner_digest = account_identity_digest(account)
    except Exception:
        _reject()
    if (
        not hmac.compare_digest(endpoint.owner_identity_digest, live_owner_digest)
        or not hmac.compare_digest(
            run.account_identity_digest,
            endpoint.owner_identity_digest,
        )
    ):
        _reject()

    accepted_key_id = accepted_marketplace_feed_capability_key_id(
        endpoint,
        provided_capability,
    )
    if not accepted_key_id or not _KEY_ID_RE.fullmatch(accepted_key_id):
        _reject()
    return accepted_key_id


def _presigned_exact_version_location(
    snapshot: _LockedPrivateFeedSnapshot,
    *,
    request_method: str,
    ttl_seconds: int,
    presign_exact_version: ExactVersionPresigner,
) -> str:
    location = presign_exact_version(
        bucket=snapshot.artifact.storage_bucket,
        object_key=snapshot.artifact.object_key,
        object_version_id=snapshot.artifact.object_version_id,
        request_method=request_method,
        expires_in=ttl_seconds,
    )
    parsed = urlsplit(str(location or ''))
    query = parse_qs(parsed.query, keep_blank_values=True)
    expected_path = (
        f'/{snapshot.artifact.storage_bucket}/{snapshot.artifact.object_key}'
    )
    if (
        parsed.scheme != 'https'
        or parsed.hostname != 'storage.yandexcloud.net'
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or unquote(parsed.path) != expected_path
        or query.get('versionId') != [snapshot.artifact.object_version_id]
        or query.get('X-Amz-Expires') != [str(ttl_seconds)]
        or len(query.get('X-Amz-Signature', [])) != 1
        or not query['X-Amz-Signature'][0]
        or parsed.fragment
    ):
        _reject()
    return location


@transaction.atomic(durable=True)
def issue_private_feed_redirect(
    *,
    public_id: uuid.UUID,
    provided_capability: object,
    request_method: str,
    presign_exact_version: ExactVersionPresigner | None = None,
) -> PrivateFeedRedirect:
    """Persist exact fetch evidence, then return a method-bound 307 location.

    No request token, query string, headers, network address, user agent, or
    generated location enters the evidence row.  An exception during signing,
    evidence insertion, or transaction commit prevents the caller from
    receiving any redirect location.
    """

    if request_method not in {'GET', 'HEAD'}:
        _reject()
    # Private-bucket credentials deliberately do not share the media storage
    # key pair.  Callers must inject the dedicated least-privilege presigner;
    # a missing callable keeps this path fail-closed.
    if presign_exact_version is None:
        _reject()
    snapshot = _lock_private_feed_snapshot(public_id)
    accepted_key_id = _validate_locked_private_snapshot(
        snapshot,
        provided_capability=provided_capability,
    )
    ttl_seconds = _configured_redirect_ttl()
    signed_at = timezone.now()
    expires_at = signed_at + timedelta(seconds=ttl_seconds)
    location = _presigned_exact_version_location(
        snapshot,
        request_method=request_method,
        ttl_seconds=ttl_seconds,
        presign_exact_version=presign_exact_version,
    )
    source_intent_revision = snapshot.run.source_intent_revision
    if source_intent_revision is None:
        _reject()
    MarketplaceFeedFetchEvidence.objects.create(
        endpoint=snapshot.endpoint,
        artifact=snapshot.artifact,
        request_method=request_method,
        accepted_token_key_id=accepted_key_id,
        capability_revision=snapshot.endpoint.capability_revision,
        endpoint_revision=snapshot.endpoint.artifact_revision,
        source_intent_revision=source_intent_revision,
        run_revision=snapshot.run.revision,
        redirect_expires_at=expires_at,
    )
    return PrivateFeedRedirect(location=location, expires_at=expires_at)
