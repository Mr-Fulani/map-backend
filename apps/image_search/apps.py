from django.apps import AppConfig


class ImageSearchConfig(AppConfig):
    """Приложение автоматического поиска изображений для товаров."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.image_search'
    verbose_name = 'Поиск изображений'

    def ready(self):
        import apps.image_search.signals  # noqa: F401
        import apps.image_search.sources.duckduckgo  # noqa: F401  — регистрирует источник в реестре
        import apps.image_search.sources.brave  # noqa: F401  — регистрирует источник в реестре
        import apps.image_search.sources.tavily  # noqa: F401  — управляемый fallback
