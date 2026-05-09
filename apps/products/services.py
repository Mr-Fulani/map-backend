import hashlib
import json
from decimal import Decimal

from apps.products.models import Product


def _compute_hash(data: dict) -> str:
    """SHA256-хэш ключевых полей товара — используется для обнаружения изменений."""
    payload = {
        'name': data.get('name', ''),
        'brand': data.get('brand', ''),
        'price': str(data.get('price', '')),
        'stock_qty': data.get('stock_qty', 0),
        'category': data.get('category', ''),
        'condition': data.get('condition', 'new'),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ProductService:
    """Сервис управления товарами: создание/обновление из источников данных."""

    @staticmethod
    def upsert_from_source(tenant, datasource, data: dict) -> tuple[Product, str]:
        """
        Создаёт или обновляет товар из данных адаптера.

        Возвращает (product, status) где status: 'created' | 'updated' | 'unchanged'.
        Unchanged означает что данные не изменились — задача в Celery не нужна.
        """
        hash_new = _compute_hash(data)
        uuid_1c = data.get('uuid') or None

        lookup = {'tenant': tenant, 'datasource': datasource, 'article': data['article']}
        defaults = {
            'name': data.get('name', ''),
            'brand': data.get('brand', ''),
            'category_1c': data.get('category', ''),
            'condition': data.get('condition', Product.CONDITION_NEW),
            'price': Decimal(str(data.get('price', '0'))),
            'stock_qty': int(data.get('stock_qty', 0)),
            'warehouse': data.get('warehouse', ''),
            'description_1c': data.get('description', ''),
            'hash_1c': hash_new,
        }
        if uuid_1c is not None:
            defaults['uuid_1c'] = uuid_1c

        # Читаем старый хэш ДО update_or_create — иначе всегда будет 'unchanged'
        try:
            existing = Product.objects.get(**lookup)
            old_hash = existing.hash_1c
        except Product.DoesNotExist:
            existing = None
            old_hash = None

        product, created = Product.objects.update_or_create(**lookup, defaults=defaults)
        if created:
            return product, 'created'
        if old_hash != hash_new:
            return product, 'updated'
        return product, 'unchanged'

    @staticmethod
    def detect_change_type(old_data: dict, new_data: dict) -> str:
        """
        Определяет тип изменения товара.

        Нужно для решения: надо ли перегенерировать описание и как обновить листинг.
        Возвращает: 'price_only' | 'stock_only' | 'content' | 'category'
        """
        price_changed = str(old_data.get('price')) != str(new_data.get('price'))
        stock_changed = old_data.get('stock_qty') != new_data.get('stock_qty')
        category_changed = old_data.get('category') != new_data.get('category')

        content_fields = {'name', 'brand', 'condition', 'description'}
        content_changed = any(old_data.get(f) != new_data.get(f) for f in content_fields)

        if category_changed:
            return 'category'
        if content_changed:
            return 'content'
        if price_changed and not stock_changed:
            return 'price_only'
        if stock_changed and not price_changed:
            return 'stock_only'
        return 'content'
