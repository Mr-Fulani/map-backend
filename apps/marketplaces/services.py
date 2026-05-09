from apps.marketplaces.models import CategoryMapping, Listing


class CategoryMappingService:
    """Сервис маппинга категорий из источников данных в категории Avito."""

    @staticmethod
    def get_or_suggest(tenant, category_1c: str) -> CategoryMapping | None:
        """Возвращает маппинг для категории или None если не найден."""
        return CategoryMapping.objects.filter(
            tenant=tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
            category_source=category_1c,
        ).first()

    @staticmethod
    def bulk_create_from_dict(tenant, mappings: dict) -> list[CategoryMapping]:
        """
        Создаёт или обновляет маппинги из словаря {source: {target, category_id, ...}}.

        Идемпотентен — повторный вызов с теми же данными не создаёт дублей.
        """
        result = []
        for source, data in mappings.items():
            obj, _ = CategoryMapping.objects.update_or_create(
                tenant=tenant,
                marketplace=CategoryMapping.MARKETPLACE_AVITO,
                category_source=source,
                defaults={
                    'category_target': data['category_target'],
                    'category_id': data['category_id'],
                    'attributes_map': data.get('attributes_map', {}),
                },
            )
            result.append(obj)
        return result

    @staticmethod
    def get_unmapped_categories(tenant) -> list[str]:
        """Возвращает категории из товаров тенанта, для которых ещё нет маппинга."""
        from apps.products.models import Product
        mapped = set(
            CategoryMapping.objects.filter(tenant=tenant, marketplace=CategoryMapping.MARKETPLACE_AVITO)
            .values_list('category_source', flat=True)
        )
        all_categories = set(
            Product.objects.filter(tenant=tenant)
            .exclude(category_1c='')
            .values_list('category_1c', flat=True)
            .distinct()
        )
        return sorted(all_categories - mapped)


class ListingService:
    """Сервис управления объявлениями: создание, маршрутизация по типу изменения."""

    @staticmethod
    def create_or_update(product, account, change_type: str = 'content') -> Listing:
        """
        Создаёт листинг или обновляет существующий в зависимости от change_type.

        price_only → только обновить цену (минимальный запрос к Avito).
        Иначе → полное обновление или первичная публикация.
        Задача в Celery ставится через transaction.on_commit — не раньше коммита.
        """
        from django.db import transaction
        listing, created = Listing.objects.get_or_create(
            tenant=product.tenant,
            product=product,
            account=account,
            defaults={
                'price_on_listing': product.price,
                'title': product.name[:300],
                'status': Listing.STATUS_DRAFT,
            },
        )

        if not created:
            if change_type == 'price_only':
                listing.price_on_listing = product.price
                listing.save(update_fields=['price_on_listing'])
                transaction.on_commit(lambda: _enqueue_price_update(listing.pk))
                return listing

            listing.price_on_listing = product.price
            listing.save(update_fields=['price_on_listing'])

        transaction.on_commit(lambda: _enqueue_publish_or_update(listing.pk, created))
        return listing


def _enqueue_publish_or_update(listing_id: int, is_new: bool) -> None:
    """Ставит задачу публикации или обновления листинга в Celery."""
    from apps.marketplaces.tasks import publish_listing_task, update_listing_task
    if is_new:
        publish_listing_task.delay(listing_id)
    else:
        update_listing_task.delay(listing_id)


def _enqueue_price_update(listing_id: int) -> None:
    """Ставит задачу обновления цены листинга в Celery."""
    from apps.marketplaces.tasks import update_price_task
    update_price_task.delay(listing_id)
