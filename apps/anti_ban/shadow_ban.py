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
        """
        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter

        try:
            stats = AvitoAdapter(account).get_stats(days=7)
        except Exception as exc:
            return {'shadow_ban_suspected': False, 'error': str(exc)}

        total_views = stats.get('total_views', 0)
        total_clicks = stats.get('total_clicks', 0)
        avg_ctr = total_clicks / max(total_views, 1)

        suspected = total_views >= MIN_VIEWS and avg_ctr < CTR_THRESHOLD

        if suspected:
            self._log_warning(account, avg_ctr, total_views)

        return {
            'shadow_ban_suspected': suspected,
            'ctr': round(avg_ctr, 4),
            'views': total_views,
        }

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
