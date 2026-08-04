from django.apps import AppConfig


class MediaProcessingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.media_processing'
    verbose_name = 'Обработка медиа'

    def ready(self):
        from apps.media_processing import signals  # noqa: F401
