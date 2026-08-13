"""Fail-closed throttles and daily budgets for paid provider starts."""

import logging
import math
from datetime import datetime, time, timedelta

from django.core.cache import caches
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, Throttled
from rest_framework.throttling import ScopedRateThrottle


logger = logging.getLogger(__name__)

coordination_cache = caches['coordination']


class CoordinationBackendUnavailable(APIException):
    """Reject paid work when the shared coordination backend is unavailable."""

    status_code = 503
    default_detail = 'Сервис временно не может проверить лимиты запуска.'
    default_code = 'coordination_backend_unavailable'


class _CoordinationScopedRateThrottle(ScopedRateThrottle):
    """Scoped DRF throttle stored in the durable coordination cache."""

    cache = coordination_cache
    methods_attr = 'expensive_throttle_methods'

    def allow_request(self, request, view):
        methods = getattr(view, self.methods_attr, {'POST'})
        if request.method.upper() not in methods:
            return True
        try:
            return super().allow_request(request, view)
        except APIException:
            raise
        except Exception as exc:
            logger.exception('Coordination throttle backend is unavailable')
            raise CoordinationBackendUnavailable() from exc


class PrincipalScopedRateThrottle(_CoordinationScopedRateThrottle):
    """Rate-limit a concrete API key or JWT user, never the whole IP pool."""

    scope_attr = 'principal_throttle_scope'

    def get_cache_key(self, request, view):
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            if getattr(user, 'is_api_key', False):
                ident = f'api-key:{user.api_key_id}'
            else:
                ident = f'user:{user.pk}'
        else:
            ident = f'ip:{self.get_ident(request)}'
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class TenantScopedRateThrottle(_CoordinationScopedRateThrottle):
    """Shared tenant ceiling prevents many keys bypassing provider limits."""

    scope_attr = 'tenant_throttle_scope'

    def get_cache_key(self, request, view):
        tenant = getattr(request, 'tenant', None)
        tenant_id = getattr(tenant, 'pk', None)
        if tenant_id is None:
            return None
        return self.cache_format % {
            'scope': self.scope,
            'ident': f'tenant:{tenant_id}',
        }


def _seconds_until_next_local_day() -> int:
    local_now = timezone.localtime()
    next_day = local_now.date() + timedelta(days=1)
    next_midnight = timezone.make_aware(
        datetime.combine(next_day, time.min),
        timezone.get_current_timezone(),
    )
    return max(1, math.ceil((next_midnight - local_now).total_seconds()))


def consume_tenant_daily_budget(
    *, tenant_id: int, scope: str, cost: int, limit: int,
) -> int:
    """Atomically consume a tenant/day budget in the coordination cache.

    Redis ``INCR`` provides the hard shared boundary. Requests fail closed when
    the backend cannot prove that capacity remains.
    """
    cost = int(cost)
    limit = int(limit)
    if cost <= 0:
        return 0
    if limit <= 0:
        raise ValueError('Daily budget limit must be positive.')

    local_date = timezone.localdate().isoformat()
    cache_key = f'expensive-budget:{scope}:tenant:{tenant_id}:{local_date}'
    wait_seconds = _seconds_until_next_local_day()
    timeout = wait_seconds + 60
    try:
        coordination_cache.add(cache_key, 0, timeout=timeout)
        total = coordination_cache.incr(cache_key, cost)
    except Exception as exc:
        logger.exception('Coordination daily-budget backend is unavailable')
        raise CoordinationBackendUnavailable() from exc

    if total > limit:
        try:
            coordination_cache.decr(cache_key, cost)
        except Exception:
            # The request is still denied; the conservative counter is safer
            # than accidentally granting work while Redis is unhealthy.
            logger.exception('Could not roll back rejected daily-budget charge')
        raise Throttled(
            wait=wait_seconds,
            detail='Дневной лимит платных запусков организации исчерпан.',
        )
    return total


def consume_transactional_tenant_daily_budget(
    *, tenant, scope: str, cost: int, limit: int,
) -> int:
    """Consume a daily budget in the caller's database transaction.

    The caller must have opened the canonical domain transaction. Unlike the
    Redis ingress throttle, this charge rolls back together with the domain
    row and durable dispatch. The tenant lock is acquired here as a final
    concurrency guard; callers should take the same lock first when they also
    lock product/listing rows.
    """
    from apps.core.models import TenantDailyPaidUsage

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'Transactional tenant budget requires an outer database transaction.'
        )
    cost = int(cost)
    limit = int(limit)
    if cost <= 0:
        return 0
    if limit <= 0:
        raise ValueError('Daily budget limit must be positive.')
    tenant = type(tenant).objects.select_for_update().only('pk').get(pk=tenant.pk)
    usage, _ = TenantDailyPaidUsage.objects.get_or_create(
        tenant=tenant,
        scope=str(scope)[:80],
        usage_date=timezone.localdate(),
    )
    usage = TenantDailyPaidUsage.objects.select_for_update().get(pk=usage.pk)
    new_total = usage.units + cost
    if new_total > limit:
        raise Throttled(
            wait=_seconds_until_next_local_day(),
            detail='Дневной лимит платных запусков организации исчерпан.',
        )
    usage.units = new_total
    usage.save(update_fields=['units', 'updated_at'])
    return new_total
