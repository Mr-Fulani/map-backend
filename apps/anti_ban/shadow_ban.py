from datetime import timedelta

from django.utils.timezone import localdate


CTR_THRESHOLD = 0.005   # 0.5% — ниже этого значения при >500 просмотрах аномалия
MIN_VIEWS = 500         # Минимум просмотров для надёжного вывода


class ShadowBanDetector:
    """
    Анализирует статистику Avito для детекции теневого бана аккаунта.

    Теневой бан: объявления видны продавцу, но не показываются покупателям.
    Признак: низкий CTR при достаточном числе просмотров.
    """

    def check_account(self, account) -> dict:
        """
        Проверяет аккаунт на признаки теневого бана через Avito Stats API.

        Возвращает {'shadow_ban_suspected': bool, 'ctr': float, 'views': int}.
        При подозрении на бан уведомляет тенанта через SyncLog.

        Ошибки Avito не подавляются: вызывающая Celery-задача должна выполнить
        настроенный retry, а не принять недоступность провайдера за чистый результат.
        """
        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        from apps.marketplaces.models import Listing

        item_ids = list(
            account.listings.filter(
                status=Listing.STATUS_ACTIVE,
                external_id__isnull=False,
            )
            .exclude(external_id='')
            .values_list('external_id', flat=True)
        )
        if not item_ids:
            return {
                'shadow_ban_suspected': False,
                'ctr': 0.0,
                'views': 0,
            }

        date_to = localdate()
        date_from = date_to - timedelta(days=6)
        stats = AvitoAdapter(account).get_stats(
            [str(item_id) for item_id in item_ids],
            date_from,
            date_to,
        )

        # Avito не отдаёт показы карточки отдельным полем. Сохраняем ту же
        # proxy-метрику, что и StatsService: uniqViews / views.
        total_views = 0
        total_unique_views = 0
        for item in stats:
            if not isinstance(item, dict):
                raise ValueError('Avito Stats API вернул некорректный элемент статистики.')
            daily_stats = item.get('stats')
            if daily_stats is None:
                continue
            if not isinstance(daily_stats, list):
                raise ValueError('Avito Stats API вернул некорректную дневную статистику.')
            for day in daily_stats:
                if not isinstance(day, dict):
                    raise ValueError('Avito Stats API вернул некорректный дневной снимок.')
                total_views += self._required_count(day.get('views'), 'views')
                total_unique_views += self._required_count(
                    day.get('uniqViews'),
                    'uniqViews',
                )

        avg_ctr = total_unique_views / max(total_views, 1)

        suspected = total_views >= MIN_VIEWS and avg_ctr < CTR_THRESHOLD

        if suspected:
            self._log_warning(account, avg_ctr, total_views)

        return {
            'shadow_ban_suspected': suspected,
            'ctr': round(avg_ctr, 4),
            'views': total_views,
        }

    @staticmethod
    def _required_count(value, field_name: str) -> int:
        """Принимает только неотрицательные целые счётчики из JSON Avito."""
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f'Avito Stats API вернул некорректный счётчик {field_name}.',
            )
        return value

    def _log_warning(self, account, avg_ctr: float, total_views: int) -> None:
        """Записывает предупреждение в SyncLog для уведомления тенанта."""
        try:
            from apps.sync.models import SyncLog
            SyncLog.objects.create(
                tenant=account.tenant,
                event_type=SyncLog.EVENT_ANTI_BAN,
                status=SyncLog.STATUS_WARN,
                message=(
                    f'Возможный shadow ban аккаунта «{account.name}». '
                    f'CTR: {avg_ctr:.1%} за 7 дней при {total_views} просмотрах.'
                ),
                payload={
                    'account_id': account.pk,
                    'ctr': avg_ctr,
                    'total_views': total_views,
                },
            )
        except Exception:
            pass
