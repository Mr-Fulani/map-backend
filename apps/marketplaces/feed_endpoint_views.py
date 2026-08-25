"""Unauthenticated capability endpoint used by marketplace feed fetchers."""

from __future__ import annotations

import hmac
import re
import uuid

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from apps.marketplaces.feed_endpoint import (
    legacy_bridge_target_url,
    verify_marketplace_feed_capability,
)


_CAPABILITY_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
_SERVABLE_PROFILE_STATES = (
    'bridge_ready',
    'migrating',
    'update_unknown',
    'verified',
)


def _response(status: int, *, location: str | None = None) -> HttpResponse:
    response = HttpResponse(status=status, content_type='text/plain; charset=utf-8')
    if location is not None:
        response['Location'] = location
    response['Cache-Control'] = 'no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Frame-Options'] = 'DENY'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
    return response


def _not_found() -> HttpResponse:
    return _response(404)


@csrf_exempt
@sensitive_variables()
def marketplace_feed_bridge(request):
    """Validate a stable capability and redirect to the frozen legacy feed."""

    if request.method not in {'GET', 'HEAD'}:
        response = _response(405)
        response['Allow'] = 'GET, HEAD'
        return response
    if set(request.GET) != {'id', 'key'}:
        return _not_found()
    public_ids = request.GET.getlist('id')
    provided_keys = request.GET.getlist('key')
    if len(public_ids) != 1 or len(provided_keys) != 1:
        return _not_found()

    raw_public_id = public_ids[0]
    provided_key = provided_keys[0]
    if not _CAPABILITY_RE.fullmatch(provided_key):
        return _not_found()
    try:
        public_id = uuid.UUID(raw_public_id)
    except (AttributeError, TypeError, ValueError):
        return _not_found()
    if str(public_id) != raw_public_id:
        return _not_found()

    from apps.marketplaces.models import MarketplaceFeedEndpoint

    endpoint = (
        MarketplaceFeedEndpoint.objects
        .select_related('account', 'account__tenant')
        .filter(public_id=public_id)
        .first()
    )
    if endpoint is None:
        return _not_found()

    if endpoint.storage_mode == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION:
        from apps.marketplaces.feed_artifact_clients import (
            presign_private_feed_exact_version,
        )
        from apps.marketplaces.feed_artifact_serving import (
            issue_private_feed_redirect,
            private_feed_route_enabled,
        )

        if not private_feed_route_enabled(endpoint):
            return _not_found()
        try:
            redirect = issue_private_feed_redirect(
                public_id=public_id,
                provided_capability=provided_key,
                request_method=request.method,
                presign_exact_version=presign_private_feed_exact_version,
            )
        except Exception:
            # The unauthenticated capability route never leaks signing,
            # storage or ownership failures to the caller.
            return _not_found()
        return _response(307, location=redirect.location)

    if (
        endpoint.storage_mode != MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
        or endpoint.serve_enabled is not True
        or endpoint.profile_state not in _SERVABLE_PROFILE_STATES
        or endpoint.account.deleted_at is not None
        or endpoint.account.is_active is not True
        or endpoint.account.tenant.is_active is not True
    ):
        return _not_found()
    from apps.marketplaces.feed_workflow import account_identity_digest

    try:
        current_owner_digest = account_identity_digest(endpoint.account)
    except Exception:
        return _not_found()
    capability_valid = verify_marketplace_feed_capability(endpoint, provided_key)
    owner_valid = hmac.compare_digest(
        str(endpoint.owner_identity_digest),
        current_owner_digest,
    )
    if not (capability_valid & owner_valid):
        return _not_found()
    target = legacy_bridge_target_url(endpoint)
    if target is None:
        return _not_found()
    return _response(307, location=target)
