from django.apps import AppConfig


class ImageSearchConfig(AppConfig):
    """Приложение автоматического поиска изображений для товаров."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.image_search'
    verbose_name = 'Поиск изображений'

    def ready(self):
        import apps.image_search.signals  # noqa: F401
