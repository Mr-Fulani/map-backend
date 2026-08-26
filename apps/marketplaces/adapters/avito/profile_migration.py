"""Fail-closed primitives for migrating one Avito Autoload profile.

This module deliberately keeps provider transport separate from the durable
workflow in :mod:`apps.marketplaces.feed_profile_migration`.  Profile GETs are
safe to repeat.  A profile POST is different: its bearer token and rate-limit
admission are prepared before the durable ``UPDATE_UNKNOWN`` boundary, then
exactly one physical HTTP request is made without the adapter's implicit 401
retry.

Capability URLs and complete provider profiles are sensitive workflow
material.  They are never present in public result objects or exception text.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.conf import settings
import requests

from apps.marketplaces.adapters.avito.adapter import (
    AVITO_API_BASE,
    AvitoAdapter,
    _avito_request,
)
from apps.marketplaces.adapters.avito.error_handler import handle_avito_error
from apps.marketplaces.feed_endpoint import (
    FeedEndpointConfigurationError,
    canonical_marketplace_feed_cdn_origin,
    legacy_bridge_target_url,
    marketplace_feed_public_url,
)
from apps.marketplaces.models import MarketplaceFeedEndpoint


_PROFILE_KEYS = frozenset({
    'agreement',
    'allow_pay_over_limit',
    'autoload_enabled',
    'feeds_data',
    'report_email',
    'schedule',
    'uploadMode',
})
_FINGERPRINT_RE = re.compile(r'^[0-9a-f]{64}$')
_MAX_PROFILE_NODES = 50_000
_MAX_PROFILE_DEPTH = 16
_MAX_FEEDS = 1_000
_MAX_BRIDGE_BYTES = 256 * 1024 * 1024
_BRIDGE_DEADLINE_SECONDS = 120.0


class AvitoProfileMigrationError(RuntimeError):
    """A provider-specific migration operation failed safely."""


class AvitoProfileValidationError(AvitoProfileMigrationError):
    """The complete provider profile is not safe to round-trip."""


class AvitoProfileTransportError(AvitoProfileMigrationError):
    """A repeatable provider read or bridge probe failed."""


class AvitoProfilePostError(AvitoProfileMigrationError):
    """The one-shot provider POST did not yield a proven safe outcome."""


@dataclass(frozen=True)
class PreparedAvitoProfilePost:
    """Opaque one-shot authorization prepared before the durable boundary."""

    access_token: str = field(repr=False)


@dataclass(frozen=True)
class ValidatedAvitoProfile:
    """A canonical, bounded profile snapshot.

    ``profile`` is intentionally excluded from repr.  Workflow results expose
    only its SHA-256 fingerprint and bounded counts.
    """

    fingerprint: str
    profile: dict = field(repr=False)


@dataclass(frozen=True)
class AvitoProfilePlan:
    """Exact source/target plan; full material and URLs stay redacted."""

    source_fingerprint: str
    target_fingerprint: str
    owned_feed_count: int
    foreign_feed_count: int
    source_object_key: str = field(repr=False)
    source_url: str = field(repr=False)
    stable_url: str = field(repr=False)
    source_profile: dict = field(repr=False)
    target_profile: dict = field(repr=False)


@dataclass(frozen=True)
class AvitoProfileObservation:
    """Lifecycle-aware classification of one current provider profile."""

    outcome: str
    source_fingerprint: str
    target_fingerprint: str
    owned_feed_count: int
    foreign_feed_count: int
    plan: AvitoProfilePlan = field(repr=False)


@dataclass(frozen=True)
class BridgeParityProof:
    """A redaction-safe proof that all three feed reads were identical."""

    content_fingerprint: str
    byte_count: int


def _validate_json_tree(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_PROFILE_NODES or depth > _MAX_PROFILE_DEPTH:
            raise AvitoProfileValidationError(
                'Avito profile exceeds the bounded structure policy.',
            )
        if current is None or isinstance(current, (bool, str, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise AvitoProfileValidationError(
                    'Avito profile contains a non-finite number.',
                )
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise AvitoProfileValidationError(
                        'Avito profile contains a non-string object key.',
                    )
                stack.append((item, depth + 1))
            continue
        raise AvitoProfileValidationError(
            'Avito profile contains a non-JSON value.',
        )


def _canonical_json_sort_key(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    )


def _profile_fingerprint_material(profile: dict) -> dict:
    """Normalize only provider-defined unordered schedule value sets."""

    material = deepcopy(profile)
    for schedule_entry in material.get('schedule', []):
        if not isinstance(schedule_entry, dict):
            continue
        for field_name in ('time_slots', 'weekdays'):
            values = schedule_entry.get(field_name)
            if isinstance(values, list):
                values.sort(key=_canonical_json_sort_key)
    return material


def validate_avito_profile(profile: object) -> ValidatedAvitoProfile:
    """Strictly validate and fingerprint the complete writable profile."""

    if not isinstance(profile, dict):
        raise AvitoProfileValidationError(
            'Avito profile must be a JSON object.',
        )
    unknown_keys = set(profile) - _PROFILE_KEYS
    if unknown_keys:
        raise AvitoProfileValidationError(
            'Avito profile contains unsupported top-level fields.',
        )
    required_keys = {
        'autoload_enabled',
        'feeds_data',
        'report_email',
        'schedule',
    }
    if not required_keys.issubset(profile):
        raise AvitoProfileValidationError(
            'Avito profile is missing required full-upsert fields.',
        )
    if not isinstance(profile.get('autoload_enabled'), bool):
        raise AvitoProfileValidationError(
            'Avito profile autoload_enabled must be explicit.',
        )
    if (
        'allow_pay_over_limit' in profile
        and not isinstance(profile['allow_pay_over_limit'], bool)
    ):
        raise AvitoProfileValidationError(
            'Avito profile allow_pay_over_limit must be explicit.',
        )
    if 'uploadMode' in profile and profile['uploadMode'] != 'auto':
        raise AvitoProfileValidationError(
            'Avito profile uploadMode is unsupported.',
        )
    if not (
        profile['report_email'] is None
        or isinstance(profile['report_email'], str)
    ):
        raise AvitoProfileValidationError(
            'Avito profile report_email is invalid.',
        )
    if not isinstance(profile['schedule'], list):
        raise AvitoProfileValidationError(
            'Avito profile schedule is invalid.',
        )
    if 'agreement' in profile and profile['agreement'] is not True:
        raise AvitoProfileValidationError(
            'Avito profile agreement is not accepted.',
        )

    feeds = profile.get('feeds_data')
    if not isinstance(feeds, list) or not 1 <= len(feeds) <= _MAX_FEEDS:
        raise AvitoProfileValidationError(
            'Avito profile feeds_data is invalid.',
        )
    for feed in feeds:
        if not isinstance(feed, dict):
            raise AvitoProfileValidationError(
                'Avito profile contains an invalid feed object.',
            )
        feed_url = feed.get('feed_url')
        if (
            not isinstance(feed_url, str)
            or not feed_url
            or feed_url != feed_url.strip()
            or len(feed_url) > 2048
        ):
            raise AvitoProfileValidationError(
                'Avito profile contains an invalid feed URL.',
            )
        if not isinstance(feed.get('feed_name'), str):
            raise AvitoProfileValidationError(
                'Avito profile contains an invalid feed name.',
            )

    _validate_json_tree(profile)
    try:
        canonical = json.dumps(
            _profile_fingerprint_material(profile),
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise AvitoProfileValidationError(
            'Avito profile cannot be canonically encoded.',
        ) from exc
    return ValidatedAvitoProfile(
        fingerprint=hashlib.sha256(canonical).hexdigest(),
        profile=deepcopy(profile),
    )


def validate_avito_profile_upsert_target(profile: object) -> ValidatedAvitoProfile:
    """Validate the stricter existing-profile POST wire contract.

    Avito's read schema permits a nullable report email while the full-upsert
    request requires a JSON string.  Migration must not invent an email or turn
    ``null`` into an empty string, so such profiles remain inspectable but are
    refused before the durable POST boundary.
    """

    snapshot = validate_avito_profile(profile)
    if not isinstance(snapshot.profile['report_email'], str):
        raise AvitoProfileValidationError(
            'Avito profile report_email is not safe for full upsert.',
        )
    return snapshot


def _trusted_url_object_key(account, raw_url: object) -> str | None:
    """Return the exact trusted legacy key owned by ``account``, if any."""

    url = str(raw_url or '')
    if (
        not url
        or url != url.strip()
        or '\\' in url
        or '%' in url
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != 'https'
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or not parsed.hostname
    ):
        return None

    path = parsed.path
    key: str | None = None
    bucket = str(getattr(settings, 'YC_S3_BUCKET', '') or '').strip()
    storage_prefix = f'/{bucket}/' if bucket else ''
    if (
        parsed.hostname.lower() == 'storage.yandexcloud.net'
        and parsed.port is None
        and storage_prefix
        and path.startswith(storage_prefix)
    ):
        key = path[len(storage_prefix):]

    try:
        cdn_origin = canonical_marketplace_feed_cdn_origin(
            getattr(settings, 'YC_CDN_DOMAIN', ''),
        )
    except FeedEndpointConfigurationError:
        cdn_origin = ''
    if key is None and cdn_origin:
        cdn = urlsplit(cdn_origin)
        if (
            parsed.hostname.lower() == (cdn.hostname or '').lower()
            and parsed.port == cdn.port
            and path.startswith('/')
        ):
            key = path[1:]

    if not key or not key.isascii():
        return None
    candidate = SimpleNamespace(
        account=account,
        legacy_object_key=key,
        legacy_profile_url=url,
    )
    return key if legacy_bridge_target_url(candidate) == url else None


def trusted_account_feed_object_key(account, raw_url: object) -> str | None:
    """Return a trusted account-owned legacy key without exposing URL rules."""

    return _trusted_url_object_key(account, raw_url)


def inspect_unprovisioned_profile(account, profile: object) -> AvitoProfilePlan:
    """Find exactly one account-owned legacy feed before endpoint creation."""

    snapshot = validate_avito_profile(profile)
    feeds = snapshot.profile['feeds_data']
    owned = [
        (index, _trusted_url_object_key(account, feed['feed_url']))
        for index, feed in enumerate(feeds)
    ]
    owned = [(index, key) for index, key in owned if key is not None]
    if len(owned) != 1:
        raise AvitoProfileValidationError(
            'Avito profile must contain exactly one owned legacy feed.',
        )
    index, object_key = owned[0]
    source_url = feeds[index]['feed_url']
    # A stable URL does not exist until an immutable endpoint UUID is created.
    return AvitoProfilePlan(
        source_fingerprint=snapshot.fingerprint,
        target_fingerprint='',
        owned_feed_count=1,
        foreign_feed_count=len(feeds) - 1,
        source_object_key=str(object_key),
        source_url=source_url,
        stable_url='',
        source_profile=snapshot.profile,
        target_profile={},
    )


def _replace_feed_url(profile: dict, index: int, url: str) -> dict:
    result = deepcopy(profile)
    result['feeds_data'][index]['feed_url'] = url
    return result


def build_profile_plan(
    *,
    account,
    profile: object,
    source_url: str,
    source_object_key: str,
    stable_url: str,
) -> AvitoProfileObservation:
    """Classify source/target exactly and reject mixed or drifting profiles."""

    trusted_key = _trusted_url_object_key(account, source_url)
    if trusted_key is None or trusted_key != source_object_key:
        raise AvitoProfileValidationError(
            'Avito profile frozen legacy locator is not trusted.',
        )
    snapshot = validate_avito_profile(profile)
    feeds = snapshot.profile['feeds_data']
    source_indexes = [
        index for index, feed in enumerate(feeds)
        if feed['feed_url'] == source_url
    ]
    stable_indexes = [
        index for index, feed in enumerate(feeds)
        if feed['feed_url'] == stable_url
    ]
    other_owned = [
        index for index, feed in enumerate(feeds)
        if (
            index not in source_indexes
            and index not in stable_indexes
            and _trusted_url_object_key(account, feed['feed_url']) is not None
        )
    ]
    if other_owned or len(source_indexes) + len(stable_indexes) != 1:
        raise AvitoProfileValidationError(
            'Avito profile owned feed is duplicated, mixed, or drifting.',
        )

    if source_indexes:
        source_index = source_indexes[0]
        source_profile = snapshot.profile
        target_profile = _replace_feed_url(source_profile, source_index, stable_url)
        outcome = 'source'
    else:
        source_index = stable_indexes[0]
        target_profile = snapshot.profile
        source_profile = _replace_feed_url(target_profile, source_index, source_url)
        outcome = 'target'

    source = validate_avito_profile(source_profile)
    target = validate_avito_profile(target_profile)
    plan = AvitoProfilePlan(
        source_fingerprint=source.fingerprint,
        target_fingerprint=target.fingerprint,
        owned_feed_count=1,
        foreign_feed_count=len(feeds) - 1,
        source_object_key=source_object_key,
        source_url=source_url,
        stable_url=stable_url,
        source_profile=source.profile,
        target_profile=target.profile,
    )
    return AvitoProfileObservation(
        outcome=outcome,
        source_fingerprint=source.fingerprint,
        target_fingerprint=target.fingerprint,
        owned_feed_count=1,
        foreign_feed_count=len(feeds) - 1,
        plan=plan,
    )


def _baseline_matches(endpoint, observation: AvitoProfileObservation) -> bool:
    state = endpoint.profile_state
    stored = str(endpoint.profile_fingerprint or '')
    if state == MarketplaceFeedEndpoint.ProfileState.VERIFIED:
        return hmac.compare_digest(stored, observation.target_fingerprint)
    return hmac.compare_digest(stored, observation.source_fingerprint)


def observe_endpoint_profile(
    endpoint,
    profile: object,
    *,
    allow_verified_source: bool = False,
) -> AvitoProfileObservation:
    """Classify a provisioned endpoint without exposing its capability URL."""

    stable_url = marketplace_feed_public_url(endpoint)
    observation = build_profile_plan(
        account=endpoint.account,
        profile=profile,
        source_url=str(endpoint.legacy_profile_url or ''),
        source_object_key=str(endpoint.legacy_object_key or ''),
        stable_url=stable_url,
    )
    if not _baseline_matches(endpoint, observation):
        raise AvitoProfileValidationError(
            'Avito profile fingerprint does not match the endpoint baseline.',
        )
    if (
        endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
        and observation.outcome != 'target'
        and not allow_verified_source
    ):
        raise AvitoProfileValidationError(
            'Verified Avito profile no longer contains the stable target.',
        )
    return observation


def is_profile_feed_configured(*, endpoint, profile: object) -> bool:
    """Return lifecycle-aware profile configuration without provider I/O."""

    try:
        state = endpoint.profile_state
        if state == MarketplaceFeedEndpoint.ProfileState.NEW:
            if endpoint.legacy_profile_url and endpoint.legacy_object_key:
                stable_url = marketplace_feed_public_url(endpoint)
                observation = build_profile_plan(
                    account=endpoint.account,
                    profile=profile,
                    source_url=endpoint.legacy_profile_url,
                    source_object_key=endpoint.legacy_object_key,
                    stable_url=stable_url,
                )
                return observation.outcome == 'source'
            inspect_unprovisioned_profile(endpoint.account, profile)
            return True

        if state == MarketplaceFeedEndpoint.ProfileState.VERIFIED:
            # Historical full-profile parity is a migration audit invariant,
            # not an ongoing health invariant.  Users may legitimately change
            # schedule, report email, or foreign feeds after verification.
            # Health still requires a strict schema, trusted frozen source,
            # and exactly one stable owned URL with no source/mixed duplicate.
            observation = build_profile_plan(
                account=endpoint.account,
                profile=profile,
                source_url=endpoint.legacy_profile_url,
                source_object_key=endpoint.legacy_object_key,
                stable_url=marketplace_feed_public_url(endpoint),
            )
            return observation.outcome == 'target'

        observation = observe_endpoint_profile(endpoint, profile)
        if state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY:
            return observation.outcome == 'source'
        if state in {
            MarketplaceFeedEndpoint.ProfileState.MIGRATING,
            MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        }:
            return observation.outcome in {'source', 'target'}
        return False
    except (AvitoProfileMigrationError, FeedEndpointConfigurationError):
        return False


class AvitoProfileMigrationClient:
    """Transport with repeatable GET and an explicitly one-shot POST."""

    def __init__(self, account):
        self.account = account
        self.adapter = AvitoAdapter(account)

    def get_profile(self) -> dict:
        try:
            profile = self.adapter.get_autoload_profile()
        except Exception:
            raise AvitoProfileTransportError(
                'Avito profile read failed.',
            ) from None
        if not isinstance(profile, dict):
            raise AvitoProfileValidationError(
                'Avito profile must be a JSON object.',
            )
        return profile

    def prepare_post(self) -> PreparedAvitoProfilePost:
        """Acquire every retryable prerequisite before UPDATE_UNKNOWN."""

        try:
            self.adapter._rl.consume(self.account, 'status')
            token = self.adapter._auth.get_token(self.account)
        except Exception:
            raise AvitoProfileTransportError(
                'Avito profile update admission failed.',
            ) from None
        if not isinstance(token, str) or not token:
            raise AvitoProfileTransportError(
                'Avito profile update authorization failed.',
            )
        return PreparedAvitoProfilePost(token)

    def post_profile_once(
        self,
        prepared: PreparedAvitoProfilePost,
        target_profile: dict,
    ) -> None:
        """Make exactly one physical POST, with no auth or network retry."""

        payload = deepcopy(target_profile)
        try:
            response = _avito_request(
                requests.post,
                f'{AVITO_API_BASE}/autoload/v2/profile',
                operation='status',
                headers={
                    'Authorization': f'Bearer {prepared.access_token}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=30,
            )
            self.adapter._rl.handle_response_headers(
                dict(response.headers),
                self.account,
            )
            handle_avito_error(response)
        except Exception:
            raise AvitoProfilePostError(
                'Avito profile update outcome requires GET-only reconciliation.',
            ) from None


def _stream_digest(
    url: str,
    *,
    expected_status: int = 200,
) -> tuple[str, int, dict[str, object]]:
    """Hash one trusted response without buffering a potentially large feed."""

    response = requests.get(
        url,
        headers={'Accept-Encoding': 'identity'},
        stream=True,
        allow_redirects=False,
        timeout=(10, 30),
    )
    try:
        if response.status_code != expected_status:
            raise AvitoProfileTransportError(
                'Feed bridge probe returned an unexpected status.',
            )
        content_encoding = str(response.headers.get('Content-Encoding', '')).lower()
        if content_encoding not in {'', 'identity'}:
            raise AvitoProfileTransportError(
                'Feed bridge probe returned an unsupported encoding.',
            )
        digest = hashlib.sha256()
        byte_count = 0
        deadline = time.monotonic() + _BRIDGE_DEADLINE_SECONDS
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if time.monotonic() > deadline:
                raise AvitoProfileTransportError(
                    'Feed bridge probe exceeded its deadline.',
                )
            if not chunk:
                continue
            byte_count += len(chunk)
            if byte_count > _MAX_BRIDGE_BYTES:
                raise AvitoProfileTransportError(
                    'Feed bridge probe exceeded its byte limit.',
                )
            digest.update(chunk)
        return digest.hexdigest(), byte_count, dict(response.headers)
    finally:
        response.close()


def _header_value(headers: dict[str, object], name: str) -> str:
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            return str(value or '')
    return ''


def _normalized_content_type(headers: dict[str, object]) -> str:
    value = _header_value(headers, 'content-type').strip().lower()
    if not value:
        raise AvitoProfileTransportError(
            'Feed bridge response has no Content-Type.',
        )
    return re.sub(r'\s+', ' ', value)


def _feed_digest(url: str) -> tuple[str, int, str]:
    try:
        digest, byte_count, headers = _stream_digest(url)
        return digest, byte_count, _normalized_content_type(headers)
    except AvitoProfileMigrationError:
        raise
    except Exception:
        raise AvitoProfileTransportError(
            'Feed bridge byte probe failed.',
        ) from None


def probe_feed_bridge_parity(endpoint) -> BridgeParityProof:
    """Prove direct-before/stable/direct-after byte parity and exact 307."""

    direct_url = str(endpoint.legacy_profile_url or '')
    if _trusted_url_object_key(endpoint.account, direct_url) != endpoint.legacy_object_key:
        raise AvitoProfileValidationError(
            'Feed bridge legacy locator is not trusted.',
        )
    stable_url = marketplace_feed_public_url(endpoint)

    before = _feed_digest(direct_url)
    try:
        _digest, _size, redirect_headers = _stream_digest(
            stable_url,
            expected_status=307,
        )
        location = _header_value(redirect_headers, 'location')
    except AvitoProfileMigrationError:
        raise
    except Exception:
        raise AvitoProfileTransportError(
            'Feed bridge redirect probe failed.',
        ) from None
    if location != direct_url:
        raise AvitoProfileValidationError(
            'Feed bridge redirect target does not match the frozen locator.',
        )
    through_stable = _feed_digest(location)
    after = _feed_digest(direct_url)
    if before != through_stable or before != after:
        raise AvitoProfileValidationError(
            'Feed bridge direct/stable/direct byte parity failed.',
        )
    return BridgeParityProof(
        content_fingerprint=before[0],
        byte_count=before[1],
    )
