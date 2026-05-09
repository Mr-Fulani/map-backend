from django.core.cache import cache
from django.utils.timezone import now


# Лимиты операций в час per account
HOURLY_LIMITS = {
    'publish': 50,
    'update':  200,
    'delete':  30,
}


def _hour_bucket() -> str:
    """Возвращает строку с текущим часом — ключ для почасового счётчика."""
    return now().strftime('%Y%m%d%H')


class VelocityController:
    """
    Контролирует скорость операций per-account через Redis-счётчики.

    Предотвращает слишком быстрые запросы, которые Avito воспринимает как бот-активность.
    Счётчики хранятся в Redis с TTL 3600 секунд — автоматически сбрасываются каждый час.
    """

    def is_allowed(self, account, operation: str) -> bool:
        """
        Проверяет, не превышен ли часовой лимит для данной операции.

        При превышении записывает SyncLog(anti_ban_trigger) и возвращает False.
        """
        key = f'velocity:{account.pk}:{operation}:{_hour_bucket()}'
        # add инициализирует ключ с TTL если его нет; incr всегда безопасен после этого
        cache.add(key, 0, timeout=3600)
        count = cache.incr(key)

        limit = HOURLY_LIMITS.get(operation, 50)
        if count > limit:
            self._log_trigger(account, operation, count, limit)
            return False
        return True

    def get_current_count(self, account, operation: str) -> int:
        """Возвращает текущий счётчик операций за этот час (0 если не было)."""
        key = f'velocity:{account.pk}:{operation}:{_hour_bucket()}'
        return cache.get(key, 0)

    def _log_trigger(self, account, operation: str, count: int, limit: int) -> None:
        """Записывает событие anti_ban_trigger в SyncLog — не падает при ошибках БД."""
        try:
            from apps.sync.models import SyncLog
            SyncLog.objects.create(
                tenant=account.tenant,
                event_type=SyncLog.EVENT_ANTI_BAN,
                status=SyncLog.STATUS_WARN,
                message=(
                    f'Velocity limit exceeded: account={account.pk}, '
                    f'op={operation}, count={count}, limit={limit}'
                ),
                payload={
                    'account_id': account.pk,
                    'operation': operation,
                    'count': count,
                    'limit': limit,
                },
            )
        except Exception:
            pass
