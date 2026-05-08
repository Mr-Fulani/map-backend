from django.core.cache import cache

RATE_LIMITS = {
    'publish': {'rate': 10, 'per': 60},
    'update':  {'rate': 30, 'per': 60},
    'price':   {'rate': 60, 'per': 60},
    'delete':  {'rate': 10, 'per': 60},
}


class RateLimitError(Exception):
    def __init__(self, retry_after: int = 30):
        self.retry_after = retry_after
        super().__init__(f'Rate limit exceeded, retry after {retry_after}s')


class AvitoRateLimiter:
    def consume(self, account, operation: str) -> None:
        if cache.get(f'avito:rl:slow:{account.pk}'):
            raise RateLimitError(retry_after=60)

        config = RATE_LIMITS.get(operation, {'rate': 10, 'per': 60})
        key = f'avito:rl:{account.pk}:{operation}'

        count = cache.get(key, 0)
        if count >= config['rate']:
            self._log_rate_limit(account, operation)
            raise RateLimitError(retry_after=config['per'])

        pipe_count = cache.get_or_set(key, 0, timeout=config['per'])
        cache.incr(key)
        if pipe_count == 0:
            cache.expire(key, config['per'])

    def handle_response_headers(self, headers: dict, account) -> None:
        remaining = headers.get('X-RateLimit-Remaining')
        reset_at = headers.get('X-RateLimit-Reset')
        if remaining is not None and int(remaining) < 5 and reset_at:
            cache.set(f'avito:rl:slow:{account.pk}', 1, timeout=int(reset_at))

    def _log_rate_limit(self, account, operation: str) -> None:
        try:
            from apps.sync.models import SyncLog
            SyncLog.objects.create(
                tenant=account.tenant,
                event_type=SyncLog.EVENT_RATE_LIMIT,
                status=SyncLog.STATUS_WARN,
                message=f'Rate limit hit: account={account.pk}, op={operation}',
                payload={'account_id': account.pk, 'operation': operation},
            )
        except Exception:
            pass
