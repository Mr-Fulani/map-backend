from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenants'
    verbose_name = 'Тенанты'

    def ready(self):
        from apps.tenants import schema  # noqa: F401
        from apps.tenants import signals  # noqa: F401
