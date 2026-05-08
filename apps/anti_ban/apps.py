from django.apps import AppConfig


class AntiBanConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.anti_ban'
    verbose_name = 'Защита от бана'
