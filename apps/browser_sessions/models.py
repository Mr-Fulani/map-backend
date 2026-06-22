from django.db import models

from apps.core.models import TimestampedModel
from apps.marketplaces.models import MarketplaceAccount


class BrowserSession(TimestampedModel):
    """Браузерная сессия для web-режима публикации на Avito через Playwright."""

    BROWSER_CHROMIUM = 'chromium'
    BROWSER_FIREFOX = 'firefox'
    BROWSER_WEBKIT = 'webkit'
    BROWSER_CHOICES = [
        (BROWSER_CHROMIUM, 'Chromium'),
        (BROWSER_FIREFOX, 'Firefox'),
        (BROWSER_WEBKIT, 'WebKit'),
    ]

    account = models.OneToOneField(
        MarketplaceAccount, on_delete=models.CASCADE,
        related_name='browser_session', verbose_name='Аккаунт',
    )
    browser_type = models.CharField(
        max_length=20, choices=BROWSER_CHOICES, verbose_name='Тип браузера',
    )
    profile_dir = models.CharField(max_length=500, verbose_name='Путь к профилю браузера')
    cookies_enc = models.BinaryField(null=True, blank=True, verbose_name='Куки (зашифровано)')
    fingerprint = models.JSONField(default=dict, verbose_name='Fingerprint')
    proxy_url_enc = models.BinaryField(null=True, blank=True, verbose_name='Прокси (зашифровано)')
    session_valid = models.BooleanField(default=False, verbose_name='Сессия активна')
    sms_pending = models.BooleanField(default=False, verbose_name='Ожидает код подтверждения')
    last_login_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний вход')

    class Meta:
        verbose_name = 'Браузерная сессия'
        verbose_name_plural = 'Браузерные сессии'

    def __str__(self):
        return f'BrowserSession [{self.browser_type}] account={self.account_id} valid={self.session_valid}'

    @staticmethod
    def browser_for_account(account_id: int) -> str:
        """Детерминированно выбирает тип браузера по id аккаунта."""
        types = [
            BrowserSession.BROWSER_CHROMIUM,
            BrowserSession.BROWSER_FIREFOX,
            BrowserSession.BROWSER_WEBKIT,
        ]
        return types[account_id % 3]
