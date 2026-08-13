"""Conservative outcome classification for paid fixed-origin HTTP providers.

Fallback is safe only when there is positive evidence that the provider did
not accept the operation.  Everything after request transmission is treated
as uncertain unless the provider returned an allowlisted, authoritative 4xx
rejection.
"""

from __future__ import annotations

from requests import exceptions as requests_exceptions


# Provider-specific response contracts. An arbitrary HTTP 4xx is not proof
# that a paid request was rejected before accounting, so undocumented statuses
# remain uncertain. 408/429 and Tavily 432/433 are deliberately excluded.
BRAVE_AUTHORITATIVE_REJECTION_STATUSES = frozenset({401, 404, 422})
TAVILY_AUTHORITATIVE_REJECTION_STATUSES = frozenset({400, 401})


def is_authoritative_provider_rejection(
    status_code: int,
    *,
    documented_statuses: frozenset[int],
) -> bool:
    """Return whether this provider documents a non-accepted response."""
    return int(status_code) in documented_statuses


def is_proven_pre_send_failure(exc: BaseException) -> bool:
    """Return whether Requests guarantees no application request was sent.

    ``ConnectTimeout`` is documented by Requests as safe to retry.  URL/schema
    failures happen while constructing the fixed-origin request.  A generic
    ``ConnectionError`` is intentionally *not* included because it also covers
    connection drops after bytes were transmitted.
    """
    return isinstance(exc, (
        requests_exceptions.ConnectTimeout,
        requests_exceptions.InvalidSchema,
        requests_exceptions.InvalidURL,
        requests_exceptions.MissingSchema,
        requests_exceptions.URLRequired,
    ))
