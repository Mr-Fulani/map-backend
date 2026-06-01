from django.db import transaction

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


class ListingNotFound(Exception):
    """Листинг тенанта не найден."""


class InvalidListingStatus(Exception):
    """Операция недопустима для текущего статуса листинга."""


class NoActiveAccounts(Exception):
    """У тенанта нет ни одного активного аккаунта маркетплейса."""


class AccountAlreadyExists(Exception):
    """Аккаунт с таким external_id уже существует у тенанта."""


class ListingService:
    """Сервис управления объявлениями: создание, маршрутизация по типу изменения."""

    @staticmethod
    def get_for_tenant(listing_id: int, tenant) -> Listing:
        """Возвращает листинг тенанта или бросает ListingNotFound."""
        try:
            return (
                Listing.objects
                .select_related('product', 'product__catalog_category', 'account')
                .prefetch_related('product__images')
                .get(pk=listing_id, tenant=tenant)
            )
        except Listing.DoesNotExist:
            raise ListingNotFound(f'Листинг {listing_id} не найден')

    @staticmethod
    def approve(listing_id: int, tenant) -> Listing:
        """
        Одобряет листинг requires_review и ставит задачу публикации в Celery.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в статусе requires_review.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status != Listing.STATUS_REQUIRES_REVIEW:
            raise InvalidListingStatus(
                f'Одобрить можно только листинг в статусе requires_review, '
                f'текущий статус: {listing.status}'
            )
        listing.status = Listing.STATUS_DRAFT
        listing.save(update_fields=['status'])

        lid = listing.pk
        transaction.on_commit(lambda: _enqueue_publish_or_update(lid, is_new=True))
        return listing

    @staticmethod
    def publish(listing_id: int, tenant) -> Listing:
        """
        Публикует черновик, отклонённый или архивный листинг на Avito.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в подходящем статусе для публикации.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        publishable = (
            Listing.STATUS_DRAFT,
            Listing.STATUS_REJECTED,
            Listing.STATUS_ARCHIVED,
        )
        if listing.status not in publishable:
            raise InvalidListingStatus(
                f'Публикация доступна для draft/rejected/archived, '
                f'текущий статус: {listing.status}'
            )
        lid = listing.pk
        transaction.on_commit(lambda: _enqueue_publish_or_update(lid, is_new=True))
        return listing

    @staticmethod
    def request_regenerate(listing_id: int, tenant) -> Listing:
        """
        Инициирует перегенерацию AI-описания.

        Доступно для статусов requires_review, draft, rejected.
        После генерации листинг автоматически публикуется (если confidence ≥ 0.5)
        или снова попадёт на проверку.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в подходящем статусе.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        regenerable = (
            Listing.STATUS_REQUIRES_REVIEW,
            Listing.STATUS_DRAFT,
            Listing.STATUS_REJECTED,
        )
        if listing.status not in regenerable:
            raise InvalidListingStatus(
                f'Перегенерация недоступна для статуса {listing.status}'
            )
        product_id = listing.product_id
        transaction.on_commit(lambda: _enqueue_ai_generation(product_id))
        return listing

    @staticmethod
    def update_content(listing_id: int, tenant, title: str | None, description_ai: str | None) -> Listing:
        """
        Обновляет заголовок и/или AI-описание листинга вручную.

        Не меняет статус — оператор редактирует текст перед одобрением.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг нельзя редактировать (active или deleted).
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status in (Listing.STATUS_ACTIVE, Listing.STATUS_DELETED):
            raise InvalidListingStatus(
                f'Нельзя редактировать листинг в статусе {listing.status}'
            )
        update_fields = []
        if title is not None:
            listing.title = title[:300]
            update_fields.append('title')
        if description_ai is not None:
            listing.description_ai = description_ai
            update_fields.append('description_ai')
        if update_fields:
            listing.save(update_fields=update_fields)
        return listing

    @staticmethod
    def archive_product(product, tenant) -> int:
        """
        Ставит задачу снятия с публикации для всех активных листингов товара тенанта.

        Возвращает количество затронутых листингов.
        """
        listings = Listing.objects.filter(
            tenant=tenant,
            product=product,
            status=Listing.STATUS_ACTIVE,
        )
        count = 0
        for listing in listings:
            lid = listing.pk
            transaction.on_commit(lambda pk=lid: _enqueue_unpublish(pk))
            count += 1
        return count

    @staticmethod
    def publish_product(product, tenant) -> list[int]:
        """
        Создаёт или обновляет листинги товара для всех активных аккаунтов тенанта.

        Raises:
            NoActiveAccounts: у тенанта нет активных аккаунтов маркетплейсов.
        """
        from apps.marketplaces.models import MarketplaceAccount
        accounts = MarketplaceAccount.objects.filter(tenant=tenant, is_active=True)
        if not accounts.exists():
            raise NoActiveAccounts('Нет подключённых активных аккаунтов')
        listing_ids = []
        for account in accounts:
            listing = ListingService.create_or_update(product, account)
            listing_ids.append(listing.pk)
        return listing_ids

    @staticmethod
    def create_or_update(product, account, change_type: str = 'content') -> Listing:
        """
        Создаёт листинг или обновляет существующий в зависимости от change_type.

        price_only → только обновить цену (минимальный запрос к Avito).
        Иначе → полное обновление или первичная публикация.
        Задача в Celery ставится через transaction.on_commit — не раньше коммита.
        """
        listing, created = Listing.objects.get_or_create(
            tenant=product.tenant,
            product=product,
            account=account,
            defaults={
                'price_on_listing': product.price,
                'title': (product.title_ai or product.name)[:300],
                'description_ai': product.description_ai,
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


class MarketplaceAccountService:
    """Сервис управления аккаунтами маркетплейсов: создание, обновление credentials."""

    @staticmethod
    def _fetch_avito_user_id(credentials_enc: str, fallback: str) -> str:
        """Получает числовой user_id из Avito API по credentials; возвращает fallback при ошибке."""
        import requests as req
        from apps.marketplaces.adapters.avito.auth import AvitoAuthManager

        class _Tmp:
            pk = None

        tmp = _Tmp()
        tmp.credentials_enc = credentials_enc
        try:
            token = AvitoAuthManager()._refresh(tmp)
            resp = req.get(
                'https://api.avito.ru/core/v1/accounts/self',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            resp.raise_for_status()
            return str(resp.json()['id'])
        except Exception:
            return fallback

    @staticmethod
    def create(tenant, data: dict):
        """
        Создаёт аккаунт маркетплейса с зашифрованными credentials.

        Автоматически запрашивает реальный Avito user_id через API.
        При конфликте external_id бросает AccountAlreadyExists.
        """
        from apps.datasources.encryption import encrypt
        from apps.marketplaces.models import MarketplaceAccount

        credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        external_id = MarketplaceAccountService._fetch_avito_user_id(
            credentials_enc, data.get('external_id', '')
        )
        try:
            return MarketplaceAccount.objects.create(
                tenant=tenant,
                name=data['name'],
                marketplace=data['marketplace'],
                external_id=external_id,
                credentials_enc=credentials_enc,
            )
        except Exception:
            raise AccountAlreadyExists('Аккаунт с таким external_id уже существует')

    @staticmethod
    def update_credentials(account, data: dict):
        """Полностью обновляет аккаунт: имя, marketplace, external_id и credentials."""
        from apps.datasources.encryption import encrypt
        account.name = data['name']
        account.marketplace = data['marketplace']
        account.external_id = data['external_id']
        account.credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        account.save(update_fields=['name', 'marketplace', 'external_id', 'credentials_enc'])
        return account

    @staticmethod
    def update_partial(account, data: dict):
        """Частично обновляет аккаунт: is_active и/или name."""
        update_fields = []
        if 'is_active' in data:
            account.is_active = bool(data['is_active'])
            update_fields.append('is_active')
        if 'name' in data:
            account.name = str(data['name'])[:200]
            update_fields.append('name')
        if update_fields:
            account.save(update_fields=update_fields)
        return account


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


def _enqueue_ai_generation(product_id: int) -> None:
    """Ставит enrichment-aware задачу генерации AI-описания в Celery."""
    from apps.products.models import Product
    from apps.products.services import ProductService

    product = Product.objects.select_related('tenant').get(pk=product_id)
    ProductService.schedule_ai_generation(product, product.tenant)


def _enqueue_unpublish(listing_id: int) -> None:
    """Ставит задачу снятия листинга с публикации в Celery."""
    from apps.marketplaces.tasks import unpublish_listing_task
    unpublish_listing_task.delay(listing_id)
