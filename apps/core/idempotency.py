"""Helpers for durable ingress idempotency contracts."""

import hashlib
import json
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder


IDEMPOTENCY_CONFLICT_MESSAGE = (
    'Ключ идемпотентности уже использован для другого запроса.'
)


class IdempotencyConflict(Exception):
    """The same domain idempotency key was reused for a different intent."""


def canonical_payload_fingerprint(payload: Any) -> str:
    """Return a stable SHA-256 digest for a JSON-compatible request intent."""
    encoded = json.dumps(
        payload,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def raise_on_fingerprint_conflict(actual: str, expected: str) -> None:
    """Reject key reuse unless it represents exactly the original intent."""
    if actual != expected:
        raise IdempotencyConflict(IDEMPOTENCY_CONFLICT_MESSAGE)
