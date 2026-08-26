"""Dedicated Yandex Object Storage clients for private feed artifacts.

The private feed key pair is deliberately isolated from the public media
credentials. Writes use a botocore client with automatic retries disabled;
reads and signatures use separate clients. Yandex may omit both owner fields
from ``GetBucketAcl``. When it supplies an owner ID, canary preflight verifies
it exactly. Otherwise the exact bucket, versioning and KMS checks remain the
external proof, while the required expected owner remains an application-level
scope token because Yandex does not implement AWS's ``ExpectedBucketOwner``
request parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

import boto3
from botocore.client import Config
from django.conf import settings


S3_ENDPOINT_URL = 'https://storage.yandexcloud.net'
S3_REGION = 'ru-central1'
YANDEX_LIST_VERSIONS_ADAPTER_POLICY_REVISION = 'yandex-list-versions-v1'

_PRIVATE_FEED_OBJECT_KEY_RE = re.compile(
    r'^private-feeds/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9]{5}/feed\.xml$',
)
_POLICY_REVISION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')


class PrivateFeedClientConfigurationError(RuntimeError):
    """The dedicated private-storage client is not safely configured."""


def _required_setting(name: str) -> str:
    value = str(getattr(settings, name, '') or '').strip()
    if not value:
        raise PrivateFeedClientConfigurationError(
            f'{name} is required for private feed artifacts.',
        )
    return value


def _client(*, total_max_attempts: int):
    session = boto3.session.Session(
        aws_access_key_id=_required_setting(
            'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID',
        ),
        aws_secret_access_key=_required_setting(
            'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY',
        ),
        region_name=S3_REGION,
    )
    return session.client(
        's3',
        endpoint_url=S3_ENDPOINT_URL,
        config=Config(
            signature_version='s3v4',
            retries={
                'mode': 'standard',
                'total_max_attempts': total_max_attempts,
            },
            s3={'addressing_style': 'path'},
        ),
    )


@dataclass(slots=True)
class YandexPrivateVersionedObjectClient:
    """One-shot PUT plus retryable exact-version reads for one fixed bucket."""

    write_client: Any
    read_client: Any
    bucket: str
    expected_bucket_owner: str
    kms_key_id: str
    put_total_max_attempts: int = 1

    def _scoped(self, kwargs: Mapping[str, object]) -> dict[str, object]:
        request = dict(kwargs)
        supplied_owner = request.pop('ExpectedBucketOwner', None)
        if supplied_owner != self.expected_bucket_owner:
            raise PrivateFeedClientConfigurationError(
                'Private feed expected bucket owner mismatch.',
            )
        if request.get('Bucket') != self.bucket:
            raise PrivateFeedClientConfigurationError(
                'Private feed bucket scope mismatch.',
            )
        return request

    def put_object_once(self, **kwargs: object) -> Mapping[str, object]:
        request = self._scoped(kwargs)
        request['ServerSideEncryption'] = 'aws:kms'
        request['SSEKMSKeyId'] = self.kms_key_id
        return self.write_client.put_object(**request)

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        return self.read_client.head_object(**self._scoped(kwargs))

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        return self.read_client.get_object(**self._scoped(kwargs))


@dataclass(slots=True)
class YandexAuthoritativeExactVersionListClient:
    """Read-only, exact-key adapter for reviewed PUT reconciliation.

    Yandex documents strong consistency for PUT/DELETE objects and exposes
    ``ListObjectVersions`` for versioned buckets.  The per-incident canary
    policy revision is supplied explicitly by the operator so the durable
    audit identifies the exact real-bucket observation that admitted absence
    reconciliation.  This adapter has no object write or delete method.
    """

    read_client: Any
    bucket: str
    expected_bucket_owner: str
    canary_policy_revision: str
    authoritative_exact_key_version_listing: bool = True
    adapter_policy_revision: str = YANDEX_LIST_VERSIONS_ADAPTER_POLICY_REVISION

    def __post_init__(self) -> None:
        if not _POLICY_REVISION_RE.fullmatch(self.canary_policy_revision):
            raise PrivateFeedClientConfigurationError(
                'Private feed reconciliation canary policy revision is invalid.',
            )

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        request = dict(kwargs)
        supplied_owner = request.pop('ExpectedBucketOwner', None)
        if supplied_owner != self.expected_bucket_owner:
            raise PrivateFeedClientConfigurationError(
                'Private feed expected bucket owner mismatch.',
            )
        if request.get('Bucket') != self.bucket:
            raise PrivateFeedClientConfigurationError(
                'Private feed bucket scope mismatch.',
            )
        prefix = request.get('Prefix')
        if not isinstance(prefix, str) or not _PRIVATE_FEED_OBJECT_KEY_RE.fullmatch(
            prefix,
        ):
            raise PrivateFeedClientConfigurationError(
                'Private feed reconciliation requires one exact object key.',
            )
        if request.get('MaxKeys') != 100:
            raise PrivateFeedClientConfigurationError(
                'Private feed reconciliation page size mismatch.',
            )
        key_marker = request.get('KeyMarker')
        version_marker = request.get('VersionIdMarker')
        if (key_marker is None) != (version_marker is None):
            raise PrivateFeedClientConfigurationError(
                'Private feed reconciliation pagination fence is incomplete.',
            )
        unexpected = set(request) - {
            'Bucket',
            'Prefix',
            'MaxKeys',
            'KeyMarker',
            'VersionIdMarker',
        }
        if unexpected:
            raise PrivateFeedClientConfigurationError(
                'Private feed reconciliation request contains unsupported fields.',
            )
        return self.read_client.list_object_versions(**request)


def private_feed_object_client() -> YandexPrivateVersionedObjectClient:
    """Build a dedicated client pair without reusing media credentials."""

    return YandexPrivateVersionedObjectClient(
        write_client=_client(total_max_attempts=1),
        read_client=_client(total_max_attempts=4),
        bucket=_required_setting('MARKETPLACE_FEED_ARTIFACT_BUCKET'),
        expected_bucket_owner=_required_setting(
            'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER',
        ),
        kms_key_id=_required_setting('MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID'),
    )


def private_feed_authoritative_version_client(
    *,
    canary_policy_revision: str,
) -> YandexAuthoritativeExactVersionListClient:
    """Build the read-only adapter used by one audited reconciliation."""

    return YandexAuthoritativeExactVersionListClient(
        read_client=_client(total_max_attempts=4),
        bucket=_required_setting('MARKETPLACE_FEED_ARTIFACT_BUCKET'),
        expected_bucket_owner=_required_setting(
            'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER',
        ),
        canary_policy_revision=canary_policy_revision,
    )


def presign_private_feed_exact_version(
    *,
    bucket: str,
    object_key: str,
    object_version_id: str,
    request_method: str,
    expires_in: int,
) -> str:
    """Sign one exact immutable object version for GET or HEAD."""

    configured_bucket = _required_setting('MARKETPLACE_FEED_ARTIFACT_BUCKET')
    if bucket != configured_bucket:
        raise PrivateFeedClientConfigurationError(
            'Private feed presigner bucket scope mismatch.',
        )
    if request_method not in {'GET', 'HEAD'}:
        raise PrivateFeedClientConfigurationError(
            'Private feed presigner accepts GET or HEAD only.',
        )
    client = _client(total_max_attempts=4)
    return client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': configured_bucket,
            'Key': object_key,
            'VersionId': object_version_id,
        },
        ExpiresIn=expires_in,
        HttpMethod=request_method,
    )


def private_feed_bucket_preflight() -> dict[str, str]:
    """Verify the private bucket contract without modifying Object Storage."""

    bucket = _required_setting('MARKETPLACE_FEED_ARTIFACT_BUCKET')
    expected_owner = _required_setting(
        'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER',
    )
    kms_key_id = _required_setting('MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID')
    client = _client(total_max_attempts=4)
    acl = client.get_bucket_acl(Bucket=bucket)
    owner = acl.get('Owner')
    if not isinstance(owner, Mapping):
        raise PrivateFeedClientConfigurationError(
            'Private feed bucket owner response is invalid.',
        )
    actual_owner = str(owner.get('ID', '') or '').strip()
    owner_display_name = str(owner.get('DisplayName', '') or '').strip()
    if actual_owner and actual_owner != expected_owner:
        raise PrivateFeedClientConfigurationError(
            'Private feed bucket folder owner does not match configuration.',
        )
    if not actual_owner and owner_display_name:
        raise PrivateFeedClientConfigurationError(
            'Private feed bucket owner response is incomplete.',
        )
    owner_check = 'verified' if actual_owner else 'unavailable'
    versioning = client.get_bucket_versioning(Bucket=bucket)
    if versioning.get('Status') != 'Enabled':
        raise PrivateFeedClientConfigurationError(
            'Private feed bucket versioning must be Enabled.',
        )
    encryption = client.get_bucket_encryption(Bucket=bucket)
    rules = (
        encryption.get('ServerSideEncryptionConfiguration', {})
        .get('Rules', [])
    )
    matches = [
        rule
        for rule in rules
        if rule.get('ApplyServerSideEncryptionByDefault', {}).get(
            'SSEAlgorithm',
        ) == 'aws:kms'
        and rule.get('ApplyServerSideEncryptionByDefault', {}).get(
            'KMSMasterKeyID',
        ) == kms_key_id
    ]
    if len(matches) != 1:
        raise PrivateFeedClientConfigurationError(
            'Private feed bucket must use the configured default KMS key.',
        )
    return {
        'bucket': bucket,
        'owner_id': actual_owner,
        'owner_check': owner_check,
        'versioning': 'Enabled',
        'kms_key_id': kms_key_id,
    }
