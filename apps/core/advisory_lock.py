"""Small PostgreSQL session-lock primitive for crash-safe workflow ownership."""

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib

from django.conf import settings
from django.db import connection


def _signed_lock_key(identity: str) -> int:
    digest = hashlib.sha256(str(identity).encode('utf-8')).digest()
    return int.from_bytes(digest[:8], byteorder='big', signed=True)


@contextmanager
def try_session_advisory_lock(identity: str) -> Iterator[bool]:
    """Yield whether this DB session owns an exact business-workflow lock.

    Session advisory locks survive inner transaction commits, which is required
    here: provider reservations/checkpoints must commit before network I/O while
    the same owner remains serialized through local domain apply. PostgreSQL
    releases the lock automatically if a hard-killed worker loses its database
    connection. Non-PostgreSQL development databases remain single-process and
    use their existing task coordination; production is PostgreSQL-only.
    """
    if connection.vendor != 'postgresql':
        if settings.DEBUG and connection.vendor == 'sqlite':
            # SQLite is used only by isolated local/unit tests. It cannot model
            # multi-process ownership and is never an accepted production DB.
            yield True
            return
        raise RuntimeError(
            'Durable provider workflows require PostgreSQL advisory locks.',
        )

    key = _signed_lock_key(identity)
    acquired = False
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [key])
        row = cursor.fetchone()
        if row is None or not isinstance(row[0], bool):
            raise RuntimeError('PostgreSQL advisory-lock result is invalid.')
        acquired = row[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [key])
                row = cursor.fetchone()
                if row is None or row[0] is not True:
                    raise RuntimeError(
                        'PostgreSQL advisory workflow lock was not released.',
                    )
