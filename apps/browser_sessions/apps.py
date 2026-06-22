from django.apps import AppConfig


class BrowserSessionsConfig(AppConfig):
    """Приложение для управления браузерными сессиями (web-режим публикации на Avito)."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.browser_sessions'
    verbose_name = 'Браузерные сессии'
