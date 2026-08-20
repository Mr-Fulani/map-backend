from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'
    verbose_name = 'Ядро'

    def ready(self):
        # Register process-local Celery signals in Django, Beat and worker
        # processes. Signal handlers are fail-open for business workloads.
        from apps.core import celery_observability  # noqa: F401
