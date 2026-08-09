"""Rate limits for public authentication endpoints."""

import hashlib
from collections.abc import Mapping

from rest_framework.throttling import ScopedRateThrottle


class CredentialScopedRateThrottle(ScopedRateThrottle):
    """Limit a normalized credential across source addresses without storing PII."""

    def get_cache_key(self, request, view):
        if not getattr(view, 'throttle_scope', None):
            return None
        payload = request.data
        if not isinstance(payload, Mapping):
            return None
        email = str(payload.get('email', '')).strip().casefold()
        if not email:
            return None
        digest = hashlib.sha256(email.encode()).hexdigest()[:24]
        return self.cache_format % {
            'scope': f'{view.throttle_scope}_credential',
            'ident': digest,
        }
