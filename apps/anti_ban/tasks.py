from celery import shared_task

from apps.anti_ban.shadow_ban import ShadowBanDetector


@shared_task(bind=True, max_retries=2, default_retry_delay=300, queue='notifications')
def check_shadow_ban_task(self, account_id: int):
    """
    Проверяет аккаунт на теневой бан через Avito Stats API.

    Запускается раз в сутки через Celery Beat.
    При подозрении на бан записывает предупреждение в SyncLog.
    """
    from apps.marketplaces.models import MarketplaceAccount

    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(pk=account_id)
    except MarketplaceAccount.DoesNotExist:
        return {'error': f'Account {account_id} not found'}

    if not account.is_active:
        return {'skipped': True, 'reason': 'account inactive'}

    try:
        result = ShadowBanDetector().check_account(account)
        return result
    except Exception as exc:
        raise self.retry(exc=exc)
