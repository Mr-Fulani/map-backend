import logging
import secrets

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


logger = logging.getLogger(__name__)


def _database_is_ready():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        return cursor.fetchone() == (1,)


def _cache_is_ready():
    key = f'healthcheck:{secrets.token_hex(8)}'
    expected = secrets.token_hex(8)
    cache.set(key, expected, timeout=10)
    try:
        return cache.get(key) == expected
    finally:
        cache.delete(key)


@require_GET
@never_cache
def health_check(request):
    """Backward-compatible liveness endpoint without dependency checks."""
    return JsonResponse({'status': 'ok'})


@require_GET
@never_cache
def readiness_check(request):
    """Return 200 only when request-critical dependencies are usable."""
    try:
        ready = _database_is_ready() and _cache_is_ready()
    except Exception as exc:
        # Backend exceptions can contain credential-bearing connection strings.
        logger.warning(
            'Readiness dependency check failed (%s).',
            type(exc).__name__,
        )
        ready = False

    return JsonResponse(
        {'status': 'ready' if ready else 'unavailable'},
        status=200 if ready else 503,
    )


urlpatterns = [
    path('health/', health_check, name='health-check'),
    path('live/', health_check, name='liveness-check'),
    path('ready/', readiness_check, name='readiness-check'),
]
