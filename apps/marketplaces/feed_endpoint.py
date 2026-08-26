"""Stable capability URLs for marketplace feed bridge endpoints.

The public URL contains no mutable tenant or account names.  Its capability is
derived from a dedicated, rotatable HMAC key ring and is never stored in the
database.  D1 deliberately supports only the legacy bridge target; immutable
private generation objects are a later storage contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from urllib.parse import urlencode, urlsplit

from django.conf import settings


PUBLIC_FEED_PATH = '/marketplace-feeds/v1/feed.xml'
_CAPABILITY_DOMAIN = b'map.marketplace-feed-url.v1\x00'
_CAPABILITY_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
_KEY_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')
_MIN_SIGNING_KEY_BYTES = 32
_MAX_SIGNING_KEY_BYTES = 1024
_OWNER_IDENTITY_RE = re.compile(r'^[0-9a-f]{64}$')
_DNS_LABEL_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')
_MAX_CAPABILITY_REVISION = (1 << 63) - 1


class FeedEndpointConfigurationError(ValueError):
    """The bridge cannot safely derive or validate its public URL."""


class _DuplicateSigningKey(ValueError):
    pass


def _keyring_object(pairs):
    """Normalize root keys while rejecting ambiguous JSON duplicates."""

    result = {}
    for raw_key_id, value in pairs:
        key_id = str(raw_key_id or '').strip()
        if key_id in result:
            raise _DuplicateSigningKey(
                'MARKETPLACE_FEED_URL_SIGNING_KEYS contains a duplicate key id.',
            )
        result[key_id] = value
    return result


def parse_marketplace_feed_url_signing_keys(raw_value: object) -> dict[str, bytes]:
    """Parse a JSON ``key_id -> base64url secret`` map without exposing keys.

    Empty input is an empty key ring so ``legacy_public`` development remains
    dark by default.  Stable bridge mode validates that the ring and primary
    key are present in production settings.
    """

    raw = str(raw_value or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw, object_pairs_hook=_keyring_object)
    except _DuplicateSigningKey as exc:
        raise ValueError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(
            'MARKETPLACE_FEED_URL_SIGNING_KEYS must be a JSON object.',
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            'MARKETPLACE_FEED_URL_SIGNING_KEYS must be a JSON object.',
        )

    result: dict[str, bytes] = {}
    for raw_key_id, encoded_secret in parsed.items():
        key_id = str(raw_key_id or '').strip()
        if not _KEY_ID_RE.fullmatch(key_id):
            raise ValueError(
                'MARKETPLACE_FEED_URL_SIGNING_KEYS contains an invalid key id.',
            )
        if not isinstance(encoded_secret, str) or not encoded_secret:
            raise ValueError(
                f'MARKETPLACE_FEED_URL_SIGNING_KEYS[{key_id!r}] is invalid.',
            )
        encoded = encoded_secret.strip()
        if not encoded or not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', encoded):
            raise ValueError(
                f'MARKETPLACE_FEED_URL_SIGNING_KEYS[{key_id!r}] is invalid.',
            )
        try:
            padding = '=' * (-len(encoded) % 4)
            secret = base64.b64decode(
                f'{encoded}{padding}',
                altchars=b'-_',
                validate=True,
            )
        except ValueError as exc:
            raise ValueError(
                f'MARKETPLACE_FEED_URL_SIGNING_KEYS[{key_id!r}] is invalid.',
            ) from exc
        if not _MIN_SIGNING_KEY_BYTES <= len(secret) <= _MAX_SIGNING_KEY_BYTES:
            raise ValueError(
                f'MARKETPLACE_FEED_URL_SIGNING_KEYS[{key_id!r}] must decode '
                f'to at least {_MIN_SIGNING_KEY_BYTES} bytes.',
            )
        result[key_id] = secret
    return result


def _endpoint_identity(endpoint) -> tuple[uuid.UUID, int, int]:
    try:
        public_id = uuid.UUID(str(endpoint.public_id))
        account_id = int(endpoint.account_id)
        tenant_id = int(endpoint.account.tenant_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise FeedEndpointConfigurationError(
            'Marketplace feed endpoint identity is incomplete.',
        ) from exc
    if account_id <= 0 or tenant_id <= 0:
        raise FeedEndpointConfigurationError(
            'Marketplace feed endpoint identity is invalid.',
        )
    return public_id, tenant_id, account_id


def _capability_message(endpoint) -> bytes:
    public_id, tenant_id, account_id = _endpoint_identity(endpoint)
    owner_digest = str(
        getattr(endpoint, 'owner_identity_digest', '') or '',
    )
    raw_revision = getattr(endpoint, 'capability_revision', None)
    if not _OWNER_IDENTITY_RE.fullmatch(owner_digest):
        raise FeedEndpointConfigurationError(
            'Marketplace feed endpoint owner identity is invalid.',
        )
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
        raise FeedEndpointConfigurationError(
            'Marketplace feed endpoint capability revision is invalid.',
        )
    capability_revision = raw_revision
    if not 1 <= capability_revision <= _MAX_CAPABILITY_REVISION:
        raise FeedEndpointConfigurationError(
            'Marketplace feed endpoint capability revision is invalid.',
        )
    return b''.join((
        _CAPABILITY_DOMAIN,
        public_id.bytes,
        b'\x00',
        str(tenant_id).encode('ascii'),
        b'\x00',
        str(account_id).encode('ascii'),
        b'\x00',
        bytes.fromhex(owner_digest),
        b'\x00',
        str(capability_revision).encode('ascii'),
    ))


def _signing_key(endpoint, key_id: object) -> bytes:
    key_id = str(key_id or '')
    if not _KEY_ID_RE.fullmatch(key_id):
        raise FeedEndpointConfigurationError(
            'Marketplace feed URL signing key id is invalid.',
        )
    keyring = getattr(settings, 'MARKETPLACE_FEED_URL_SIGNING_KEYS', {})
    if not isinstance(keyring, Mapping):
        raise FeedEndpointConfigurationError(
            'Marketplace feed URL signing key ring is invalid.',
        )
    key = keyring.get(key_id)
    if (
        not isinstance(key, bytes)
        or not _MIN_SIGNING_KEY_BYTES <= len(key) <= _MAX_SIGNING_KEY_BYTES
    ):
        raise FeedEndpointConfigurationError(
            'Marketplace feed URL signing key is unavailable.',
        )
    return key


def _capability_for_key(endpoint, key_id: object, message: bytes) -> str:
    digest = hmac.new(
        _signing_key(endpoint, key_id),
        message,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def marketplace_feed_capability(endpoint) -> str:
    """Return the canonical unpadded HMAC capability for one endpoint."""

    return _capability_for_key(
        endpoint,
        getattr(endpoint, 'token_key_id', ''),
        _capability_message(endpoint),
    )


def accepted_marketplace_feed_capability_key_id(
    endpoint,
    provided: object,
) -> str | None:
    """Return the accepted key id without ever persisting capability material.

    Both configured generations are evaluated before choosing a result.  This
    keeps the bounded rotation verifier's timing independent of whether the
    current or previous key accepted the capability.
    """

    candidate = str(provided or '')
    if not _CAPABILITY_RE.fullmatch(candidate):
        return None
    try:
        message = _capability_message(endpoint)
        current_key_id = str(getattr(endpoint, 'token_key_id', '') or '')
        current = _capability_for_key(
            endpoint,
            current_key_id,
            message,
        )
        previous_key_id = str(
            getattr(endpoint, 'previous_token_key_id', '') or '',
        )
        previous = (
            _capability_for_key(endpoint, previous_key_id, message)
            if previous_key_id
            else None
        )
    except FeedEndpointConfigurationError:
        return None
    current_match = hmac.compare_digest(current, candidate)
    previous_match = (
        hmac.compare_digest(previous, candidate)
        if previous is not None
        else False
    )
    if current_match:
        return current_key_id
    if previous_match:
        return previous_key_id
    return None


def verify_marketplace_feed_capability(endpoint, provided: object) -> bool:
    """Constant-time verification with fail-closed configuration handling."""

    return accepted_marketplace_feed_capability_key_id(endpoint, provided) is not None


def _valid_dns_hostname(hostname: str) -> bool:
    if len(hostname) > 253 or hostname.endswith('.'):
        return False
    return all(
        _DNS_LABEL_RE.fullmatch(label) is not None
        for label in hostname.split('.')
    )


def _parse_canonical_https_url(raw_value: object, *, path: str):
    raw = str(raw_value or '')
    if (
        not raw
        or raw != raw.strip()
        or '\\' in raw
        or any(character.isspace() or ord(character) < 32 for character in raw)
    ):
        raise FeedEndpointConfigurationError(
            'Marketplace feed HTTPS URL is invalid.',
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise FeedEndpointConfigurationError(
            'Marketplace feed HTTPS URL is invalid.',
        ) from exc
    hostname = parsed.hostname or ''
    if (
        parsed.scheme != 'https'
        or not _valid_dns_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != path
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or parsed.netloc.endswith(':')
    ):
        raise FeedEndpointConfigurationError(
            'Marketplace feed HTTPS URL is invalid.',
        )
    return parsed


def canonical_marketplace_feed_cdn_origin(raw_value: object) -> str:
    """Return a validated HTTPS origin for a configured CDN authority."""

    authority = str(raw_value or '')
    if not authority:
        return ''
    parsed = _parse_canonical_https_url(f'https://{authority}', path='')
    port_suffix = ':443' if parsed.port == 443 else ''
    return f'https://{parsed.hostname.lower()}{port_suffix}'


def canonical_marketplace_feed_public_base_url(raw_value: object) -> str:
    """Return a validated stable endpoint base URL without a query string."""

    raw = str(raw_value or '')
    parsed = _parse_canonical_https_url(raw, path=PUBLIC_FEED_PATH)
    return f'https://{parsed.hostname.lower()}{PUBLIC_FEED_PATH}'


def marketplace_feed_public_url(endpoint) -> str:
    """Build the stable URL used in the marketplace Autoload profile."""

    base_url = str(
        getattr(settings, 'MARKETPLACE_FEED_PUBLIC_BASE_URL', '') or '',
    ).strip()
    try:
        base_url = canonical_marketplace_feed_public_base_url(base_url)
    except FeedEndpointConfigurationError as exc:
        raise FeedEndpointConfigurationError(
            'Marketplace feed public base URL is invalid.',
        ) from exc
    query = urlencode({
        'id': str(uuid.UUID(str(endpoint.public_id))),
        'key': marketplace_feed_capability(endpoint),
    })
    return f'{base_url}?{query}'


def legacy_bridge_target_url(endpoint) -> str | None:
    """Return an exact trusted legacy object URL, never an open redirect.

    The persisted locator freezes the legacy object across account/tenant
    renames.  The route still re-derives the only allowed public URL from the
    configured bucket/CDN and exact object key before emitting ``Location``.
    """

    key = str(getattr(endpoint, 'legacy_object_key', '') or '').strip()
    target = str(getattr(endpoint, 'legacy_profile_url', '') or '').strip()
    account = getattr(endpoint, 'account', None)
    if not key or not target or account is None:
        return None
    if (
        key.startswith('/')
        or '\\' in key
        or '?' in key
        or '#' in key
        or any(ord(character) < 32 or ord(character) == 127 for character in key)
        or any(part in {'', '.', '..'} for part in key.split('/'))
    ):
        return None

    prefix = str(getattr(settings, 'MEDIA_KEY_PREFIX', '') or '').strip('/')
    # Feeds created before MEDIA_KEY_PREFIX was introduced remain owned MAP
    # objects.  Accept only the historical root plus the currently configured
    # root; the bucket, marketplace segment and account suffix still have to
    # match exactly below.
    feed_prefixes = {'feeds/'}
    if prefix:
        feed_prefixes.add(f'{prefix}/feeds/')
    marketplace = str(getattr(account, 'marketplace', '') or '').strip()
    if (
        not any(key.startswith(feed_prefix) for feed_prefix in feed_prefixes)
        or not marketplace
        or f'/{marketplace}/' not in key
        or not key.endswith(f'-{account.pk}/feed.xml')
    ):
        return None

    trusted_targets: set[str] = set()
    bucket = str(getattr(settings, 'YC_S3_BUCKET', '') or '').strip()
    if (
        3 <= len(bucket) <= 63
        and _valid_dns_hostname(bucket)
        and '/' not in bucket
        and not any(character.isspace() for character in bucket)
    ):
        trusted_targets.add(
            f'https://storage.yandexcloud.net/{bucket}/{key}',
        )
    try:
        cdn_origin = canonical_marketplace_feed_cdn_origin(
            getattr(settings, 'YC_CDN_DOMAIN', ''),
        )
    except FeedEndpointConfigurationError:
        cdn_origin = ''
    if cdn_origin:
        trusted_targets.add(f'{cdn_origin}/{key}')
    return target if target in trusted_targets else None
