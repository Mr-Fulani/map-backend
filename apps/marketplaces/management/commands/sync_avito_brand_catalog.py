"""Management command: обновление локального каталога брендов Avito.

Скачивает справочник значений поля Brand («Производитель») из user-docs API
Avito в data/avito_brand_catalog.json. Каталог используется для проверки
бренда товара перед публикацией (см. adapters/avito/brand_catalog.py).
"""
from django.core.management.base import BaseCommand, CommandError
from apps.marketplaces.adapters.avito.brand_sync import BrandCatalogSyncError, sync_brand_catalog


class Command(BaseCommand):
    """Обновляет data/avito_brand_catalog.json из user-docs API Avito."""

    help = 'Синхронизировать каталог брендов Avito (поле Brand) в локальный JSON'

    def handle(self, *args, **options):
        try:
            catalog = sync_brand_catalog()
        except BrandCatalogSyncError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f'Каталог брендов обновлён: {len(catalog.brands)} значений'
        ))
