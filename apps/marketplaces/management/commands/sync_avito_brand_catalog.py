"""Management command: обновление локального каталога брендов Avito.

Скачивает справочник значений поля Brand («Производитель») из user-docs API
Avito в data/avito_brand_catalog.json. Каталог используется для проверки
бренда товара перед публикацией (см. adapters/avito/brand_catalog.py).
"""
import json

import requests
from django.core.management.base import BaseCommand
from django.utils.timezone import now

from apps.marketplaces.adapters.avito.adapter import AVITO_API_BASE, AvitoAdapter
from apps.marketplaces.adapters.avito.brand_catalog import _CATALOG_PATH
from apps.marketplaces.models import MarketplaceAccount

SOURCE_NODE = 'transmissiia_i_privod'
BRAND_FIELD_ID = 110548


class Command(BaseCommand):
    """Обновляет data/avito_brand_catalog.json из user-docs API Avito."""

    help = 'Синхронизировать каталог брендов Avito (поле Brand) в локальный JSON'

    def handle(self, *args, **options):
        account = MarketplaceAccount.objects.filter(is_active=True).first()
        if account is None:
            self.stderr.write(self.style.ERROR('Нет активного аккаунта Avito для запроса API'))
            return

        token = AvitoAdapter(account)._auth.get_token(account)
        response = requests.get(
            f'{AVITO_API_BASE}/autoload/v1/user-docs/node/{SOURCE_NODE}/field/{BRAND_FIELD_ID}/values-json',
            headers={'Authorization': f'Bearer {token}'},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        values = payload if isinstance(payload, list) else payload.get('values', [])
        brands = sorted({
            (item.get('value') if isinstance(item, dict) else str(item))
            for item in values if item
        })
        if not brands:
            self.stderr.write(self.style.ERROR('Avito вернул пустой список брендов — файл не перезаписан'))
            return

        _CATALOG_PATH.write_text(
            json.dumps(
                {
                    'source_node': SOURCE_NODE,
                    'field_id': BRAND_FIELD_ID,
                    'synced_at': now().isoformat(),
                    'brands': brands,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding='utf-8',
        )
        self.stdout.write(self.style.SUCCESS(f'Каталог брендов обновлён: {len(brands)} значений'))
