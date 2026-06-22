import asyncio
import os

from django.core.management.base import BaseCommand

from apps.browser_sessions.stealth import generate_fingerprint


class Command(BaseCommand):
    """
    Тестирует anti-detect слой, открывая bot-detection страницу через Playwright.
    Сохраняет скриншот для визуальной проверки.

    Использование:
        python manage.py test_browser_stealth --account-id 3
        python manage.py test_browser_stealth --browser chromium
    """

    help = 'Тест stealth: открывает bot.sannysoft.com и сохраняет скриншот'

    def add_arguments(self, parser):
        parser.add_argument('--account-id', type=int, default=1, help='ID аккаунта для fingerprint')
        parser.add_argument(
            '--browser', choices=['chromium', 'firefox', 'webkit'], default=None,
            help='Тип браузера (по умолчанию — по account-id)',
        )
        parser.add_argument(
            '--url', default='https://bot.sannysoft.com',
            help='URL для теста (по умолчанию bot.sannysoft.com)',
        )
        parser.add_argument(
            '--screenshot', default='/tmp/stealth_test.png',
            help='Путь для скриншота',
        )

    def handle(self, *args, **options):
        asyncio.run(self._run(options))

    async def _run(self, options):
        from playwright.async_api import async_playwright
        from apps.browser_sessions.stealth import apply_stealth, _USER_AGENTS, _VIEWPORTS, _TIMEZONES
        import random

        account_id = options['account_id']
        fp = generate_fingerprint(account_id)
        browser_type = options['browser'] or fp['browser_type']

        self.stdout.write(f'account_id={account_id} → browser={browser_type}')
        self.stdout.write(f'viewport={fp["viewport"]}  timezone={fp["timezone_id"]}')
        self.stdout.write(f'ua={fp["user_agent"][:60]}...')

        async with async_playwright() as p:
            launcher = getattr(p, browser_type)
            launch_kwargs = {'headless': True}
            if browser_type == 'chromium':
                launch_kwargs['args'] = [
                    '--disable-blink-features=AutomationControlled',
                    '--no-first-run',
                    '--disable-infobars',
                ]

            browser = await launcher.launch(**launch_kwargs)
            context = await browser.new_context(
                user_agent=fp['user_agent'],
                viewport=fp['viewport'],
                timezone_id=fp['timezone_id'],
                locale=fp['locale'],
            )
            page = await context.new_page()
            await apply_stealth(page)

            self.stdout.write(f'Открываю {options["url"]} ...')
            await page.goto(options['url'], wait_until='networkidle', timeout=30000)

            screenshot_path = options['screenshot']
            await page.screenshot(path=screenshot_path, full_page=True)
            self.stdout.write(self.style.SUCCESS(f'Скриншот сохранён: {screenshot_path}'))

            await browser.close()
