from django.core.cache import cache

from apps.core.telemetry import metric_count

# Консервативные лимиты — уточняются по реальным заголовкам X-RateLimit-*
RATE_LIMITS = {
    'publish': {'rate': 10, 'per': 60},
    'update':  {'rate': 30, 'per': 60},
    'price':   {'rate': 60, 'per': 60},
    'delete':  {'rate': 10, 'per': 60},
}

# Пауза между повторными проверками лимита Avito Autoload «1 загрузка/час».
AUTOLOAD_RATE_LIMIT_RETRY_AFTER = 660


class RateLimitError(Exception):
    """Превышен лимит запросов к Avito API."""

    def __init__(self, retry_after: int = 30):
        self.retry_after = retry_after
        super().__init__(f'Rate limit exceeded, retry after {retry_after}s')


class AvitoRateLimiter:
    """Token bucket per (account, operation) в Redis. Адаптируется по заголовкам Avito."""

    def consume(self, account, operation: str) -> None:
        """Списывает один запрос из бакета. Бросает RateLimitError при превышении."""
        # Если Avito вернул slow-down флаг — блокируем раньше лимита
        if cache.get(f'avito:rl:slow:{account.pk}'):
            metric_count(
                'map.provider.rate_limit',
                attributes={
                    'provider': 'avito',
                    'operation': operation,
                    'rate_limit_source': 'local',
                },
            )
            raise RateLimitError(retry_after=60)

        config = RATE_LIMITS.get(operation, {'rate': 10, 'per': 60})
        key = f'avito:rl:{account.pk}:{operation}'

        try:
            count = cache.incr(key)
        except ValueError:
            # ``add`` creates the fixed window together with its TTL. If a
            # concurrent request won the race, increment that existing window.
            count = 1 if cache.add(key, 1, timeout=config['per']) else cache.incr(key)

        if count > config['rate']:
            self._log_rate_limit(account, operation)
            metric_count(
                'map.provider.rate_limit',
                attributes={
                    'provider': 'avito',
                    'operation': operation,
                    'rate_limit_source': 'local',
                },
            )
            raise RateLimitError(retry_after=config['per'])

    def handle_response_headers(self, headers: dict, account) -> None:
        """Обновляет slow-down флаг если Avito сообщил о близком исчерпании лимита."""
        remaining = headers.get('X-RateLimit-Remaining')
        reset_at = headers.get('X-RateLimit-Reset')
        if remaining is not None and int(remaining) < 5 and reset_at:
            cache.set(f'avito:rl:slow:{account.pk}', 1, timeout=int(reset_at))

    def _log_rate_limit(self, account, operation: str) -> None:
        """Записывает событие в SyncLog — не падает если таблица недоступна."""
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
