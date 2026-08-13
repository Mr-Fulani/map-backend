"""Stable timestamp helpers for retry-safe security email payloads."""

from datetime import datetime, timedelta, timezone

from django.contrib.auth.tokens import default_token_generator
from django.core import signing


_TOKEN_EPOCH = datetime(2001, 1, 1)


def current_password_reset_timestamp() -> int:
    """Return a five-minute bucket in Django's reset-token timestamp unit."""
    current = datetime.now()
    raw_timestamp = int((current - _TOKEN_EPOCH).total_seconds())
    return raw_timestamp // 300 * 300


def make_password_reset_token_at(user, timestamp: int) -> str:
    """Build the same token Django validates, with a persisted enqueue time."""
    return default_token_generator._make_token_with_timestamp(  # type: ignore[attr-defined]
        user,
        int(timestamp),
        default_token_generator.secret,
    )


def password_reset_datetime(timestamp: int) -> datetime:
    """Convert Django's 2001-based token timestamp to an aware UTC datetime."""
    return (_TOKEN_EPOCH + timedelta(seconds=int(timestamp))).replace(
        tzinfo=timezone.utc,
    )


class FixedTimestampSigner(signing.TimestampSigner):
    """TimestampSigner whose timestamp is stable across one HTTP retry bucket."""

    def __init__(self, *args, timestamp: int, **kwargs):
        super().__init__(*args, **kwargs)
        self._fixed_timestamp = int(timestamp)

    def timestamp(self) -> str:
        return signing.b62_encode(self._fixed_timestamp)


def dumps_at(value: object, *, salt: str, timestamp: int) -> str:
    """Equivalent to signing.dumps(), but with an explicit Unix timestamp."""
    return FixedTimestampSigner(
        key=None,
        salt=salt,
        timestamp=timestamp,
    ).sign_object(value, serializer=signing.JSONSerializer, compress=False)
