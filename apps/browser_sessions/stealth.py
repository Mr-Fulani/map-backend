import random

from playwright_stealth import Stealth

_USER_AGENTS = {
    'chromium': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'firefox': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) '
        'Gecko/20100101 Firefox/120.0'
    ),
    'webkit': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
    ),
}

_VIEWPORTS = [
    {'width': 1920, 'height': 1080},
    {'width': 1366, 'height': 768},
    {'width': 1440, 'height': 900},
    {'width': 1280, 'height': 800},
    {'width': 1600, 'height': 900},
]

_TIMEZONES = [
    'Europe/Moscow',
    'Europe/Samara',
    'Asia/Yekaterinburg',
    'Asia/Novosibirsk',
    'Europe/Kaliningrad',
]


def generate_fingerprint(account_id: int) -> dict:
    """
    Детерминированно генерирует fingerprint для аккаунта по его id.
    Один и тот же account_id всегда даёт одинаковый fingerprint.
    """
    rng = random.Random(account_id)
    browser_types = ['chromium', 'firefox', 'webkit']
    browser_type = browser_types[account_id % 3]
    return {
        'browser_type': browser_type,
        'user_agent': _USER_AGENTS[browser_type],
        'viewport': rng.choice(_VIEWPORTS),
        'timezone_id': rng.choice(_TIMEZONES),
        'locale': 'ru-RU',
        'canvas_noise_seed': rng.randint(1, 10000),
    }


async def apply_stealth(page) -> None:
    """Применяет playwright-stealth патчи к странице (скрывает webdriver, автоматизацию)."""
    await Stealth().apply_stealth_async(page)
