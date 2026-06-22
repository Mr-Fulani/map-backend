import asyncio
import logging
import random
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from apps.browser_sessions.stealth import apply_stealth

logger = logging.getLogger(__name__)

# Chromium требует явного отключения флагов автоматизации
_CHROMIUM_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-infobars',
    '--disable-extensions',
]


class BrowserPool:
    """
    Открывает браузер с fingerprint и stealth для конкретной BrowserSession.
    Использовать как async context manager: async with pool.acquire(session) as page.
    """

    @asynccontextmanager
    async def acquire(self, session):
        """Открывает браузер, настраивает fingerprint и stealth. Yield: playwright Page."""
        fp = session.fingerprint
        browser_type_name = session.browser_type

        async with async_playwright() as p:
            launcher = getattr(p, browser_type_name)
            launch_kwargs = {'headless': True}
            if browser_type_name == 'chromium':
                launch_kwargs['args'] = _CHROMIUM_ARGS

            browser = await launcher.launch(**launch_kwargs)
            try:
                context_kwargs = {
                    'user_agent': fp.get('user_agent'),
                    'viewport': fp.get('viewport'),
                    'timezone_id': fp.get('timezone_id'),
                    'locale': fp.get('locale', 'ru-RU'),
                }
                if session.proxy_url_enc:
                    from apps.datasources.encryption import decrypt
                    proxy_data = decrypt(bytes(session.proxy_url_enc))
                    context_kwargs['proxy'] = {'server': proxy_data['url']}

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()
                await apply_stealth(page)
                logger.debug(
                    'browser_pool: открыт %s для account=%s',
                    browser_type_name, session.account_id,
                )
                yield page
            finally:
                await browser.close()

    async def human_delay(self, min_ms: int = 80, max_ms: int = 220) -> None:
        """Случайная задержка имитирующая действия человека."""
        await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


pool = BrowserPool()
