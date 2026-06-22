import asyncio
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, queue='avito_publish')
def start_browser_login_task(self, account_id: int):
    """
    Фоновый логин на Avito через Playwright.
    Фаза 1: вводит credentials.
    Если нужна верификация — ждёт код из Redis (до 5 минут).
    Фаза 2: вводит код и сохраняет cookies.
    """
    from apps.browser_sessions.models import BrowserSession
    from apps.browser_sessions.services import BrowserSessionService
    from apps.browser_sessions.session_manager import session_manager

    try:
        session = BrowserSession.objects.select_related('account').get(account_id=account_id)
    except BrowserSession.DoesNotExist:
        logger.error('start_browser_login_task: BrowserSession не найдена для account=%s', account_id)
        return

    login, password = BrowserSessionService.get_web_credentials(session.account)
    try:
        asyncio.run(session_manager.login_with_verification(session, login, password))
    except Exception as exc:
        logger.error('start_browser_login_task: account=%s ошибка: %s', account_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60)


@shared_task(queue='avito_publish')
def refresh_browser_sessions_task():
    """
    Периодически обновляет все web-сессии.
    Запускается каждые 6 часов через Celery Beat.
    Проверяет живость сессии; если мертва — переподключает через сохранённые credentials.
    """
    from apps.browser_sessions.models import BrowserSession
    from apps.browser_sessions.services import BrowserSessionService
    from apps.browser_sessions.session_manager import session_manager
    from apps.marketplaces.models import MarketplaceAccount

    sessions = BrowserSession.objects.filter(
        account__publish_method=MarketplaceAccount.PUBLISH_WEB,
        account__is_active=True,
        cookies_enc__isnull=False,
    ).select_related('account')

    refreshed = 0
    for session in sessions:
        try:
            valid = asyncio.run(session_manager.restore_session(session))
            if not valid:
                logger.info(
                    'refresh_browser_sessions: account=%s сессия истекла, перелогиниваемся',
                    session.account_id,
                )
                start_browser_login_task.delay(session.account_id)
            refreshed += 1
        except Exception as exc:
            logger.error(
                'refresh_browser_sessions: account=%s ошибка проверки: %s',
                session.account_id, exc,
            )

    return {'checked': refreshed}
