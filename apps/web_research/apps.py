from django.apps import AppConfig


class WebResearchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.web_research'
    verbose_name = 'Интернет-исследования'

    def ready(self):
        import apps.web_research.providers.brave  # noqa: F401
