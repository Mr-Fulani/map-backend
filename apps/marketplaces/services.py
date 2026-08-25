import datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, TypedDict

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.marketplaces.listing_lifecycle import (
    clear_remote_observation,
    release_status_check,
)
from apps.marketplaces.models import (
    AvitoAccountStatus,
    CategoryMapping,
    Listing,
    ListingStats,
    MarketplaceAccount,
)
from apps.marketplaces.price_utils import (
    compute_price,
    effective_category_margin,
    effective_margin,
)


_LOCAL_STATUS_RECHECK_DELAY = datetime.timedelta(minutes=10)


class _ListingExpectedState(TypedDict):
    expected_status: str
    expected_account_id: int
    expected_external_id: str | None
    expected_deleted_at: datetime.datetime | None


def _status_lifecycle_dual_write_enabled() -> bool:
    return settings.AVITO_STATUS_LIFECYCLE_MODE == 'dual_write'


def _listing_expected_state(listing: Listing) -> _ListingExpectedState:
    return {
        'expected_status': listing.status,
        'expected_account_id': listing.account_id,
        'expected_external_id': listing.external_id,
        'expected_deleted_at': listing.deleted_at,
    }


def _local_status_due_at(listing: Listing):
    if listing.status == Listing.STATUS_ACTIVE or (
        listing.status == Listing.STATUS_ARCHIVING and bool(listing.external_id)
    ):
        return timezone.now() + _LOCAL_STATUS_RECHECK_DELAY
    return None


def _min_nudge_account_status_due(account_id: int, due_at) -> int:
    if due_at is None:
        return 0
    return MarketplaceAccount.objects.filter(
        pk=account_id,
        is_active=True,
    ).filter(
        Q(status_batch_due_at__isnull=True)
        | Q(status_batch_due_at__gt=due_at),
    ).update(status_batch_due_at=due_at)


def _copy_listing_row(target: Listing, source: Listing) -> None:
    for model_field in Listing._meta.concrete_fields:
        setattr(target, model_field.attname, getattr(source, model_field.attname))
    target._state.fields_cache.clear()


def _save_local_listing_intent(
    listing: Listing,
    update_fields,
    *,
    expected_status: str,
    expected_account_id: int,
    expected_external_id: str | None,
    expected_deleted_at,
    reset_provider_identity: bool = False,
    require_target_account_active: bool = False,
) -> bool:
    """Apply a local transition after fencing older provider observations."""

    fields = tuple(dict.fromkeys(update_fields))
    if not _status_lifecycle_dual_write_enabled():
        listing.save(update_fields=fields)
        return True

    intended_values: dict[str, object] = {}
    for field_name in fields:
        intended_values[field_name] = getattr(listing, field_name)

    if reset_provider_identity:
        listing.external_id = None
        listing.external_url = ''
        intended_values['external_id'] = None
        intended_values['external_url'] = ''
        fields = tuple(dict.fromkeys((*fields, 'external_id', 'external_url')))

    intended_account_id = listing.account_id
    account_ids = sorted({expected_account_id, intended_account_id})

    with transaction.atomic():
        locked_accounts = list(
            MarketplaceAccount.all_objects.select_for_update(of=('self',))
            .filter(
                pk__in=account_ids,
                tenant_id=listing.tenant_id,
            )
            .order_by('pk')
        )
        accounts_by_id = {account.pk: account for account in locked_accounts}
        target_account = accounts_by_id.get(intended_account_id)
        if (
            len(accounts_by_id) != len(account_ids)
            or target_account is None
            or target_account.deleted_at is not None
            or (require_target_account_active and not target_account.is_active)
        ):
            return False
        current = (
            Listing.all_objects.select_for_update(of=('self',))
            .filter(pk=listing.pk)
            .first()
        )
        if current is None or not (
            current.status == expected_status
            and current.account_id == expected_account_id
            and current.external_id == expected_external_id
            and current.deleted_at == expected_deleted_at
        ):
            if current is not None:
                _copy_listing_row(listing, current)
            return False

        for field_name, value in intended_values.items():
            setattr(current, field_name, value)
        if reset_provider_identity:
            observation_fields = clear_remote_observation().apply_to(current)
            claim_fields = release_status_check(
                next_status_check_at=None,
            ).apply_to(current)
            lifecycle_fields = tuple(dict.fromkeys((
                *observation_fields,
                *claim_fields,
            )))
        else:
            lifecycle_fields = release_status_check(
                next_status_check_at=_local_status_due_at(current),
            ).apply_to(current)
        saved_fields = tuple(dict.fromkeys((*fields, *lifecycle_fields)))
        current.save(update_fields=saved_fields)
        for field_name in saved_fields:
            setattr(listing, field_name, getattr(current, field_name))
        listing._state.fields_cache.clear()

        due_at = current.next_status_check_at
        if due_at is not None:
            _min_nudge_account_status_due(current.account_id, due_at)
        return True


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


class ListingAccountConflict(Exception):
    """Для товара уже есть листинг на выбранном аккаунте."""


class ListingBulkLimitExceeded(ValueError):
    """A direct bulk listing operation selected more rows than the API cap."""


class NoActiveAccounts(Exception):
    """У тенанта нет ни одного активного аккаунта маркетплейса."""


class AccountAlreadyExists(Exception):
    """Аккаунт с таким external_id уже существует у тенанта."""


class InvalidMarketplaceCredentials(Exception):
    """Credentials маркетплейса не прошли проверку через API."""


class MarketplaceAccountFeedConflict(Exception):
    """Изменение аккаунта конфликтует с активным feed-процессом."""


def _assert_account_identity_mutation_safe(account) -> None:
    """Не даёт сменить credentials поверх уже отправленного durable run."""

    from apps.marketplaces.feed_workflow import (
        FeedSubmissionOutcomeUncertain,
        assert_no_submitted_feed_owner,
    )

    try:
        assert_no_submitted_feed_owner(
            account.pk,
            reason='Marketplace account credentials or provider identity changed.',
        )
    except FeedSubmissionOutcomeUncertain:
        raise MarketplaceAccountFeedConflict(
            'Нельзя изменить подключение: результат предыдущей отправки '
            'фида ещё не подтверждён. Сначала выполните ручную сверку запуска.',
        ) from None


def _lock_marketplace_feed_endpoint(account_id: int):
    """Блокирует endpoint сразу после блокировки его аккаунта-владельца."""

    from apps.marketplaces.models import MarketplaceFeedEndpoint

    return (
        MarketplaceFeedEndpoint.objects.select_for_update()
        .filter(account_id=account_id)
        .first()
    )


def _assert_feed_endpoint_identity_mutation_safe(endpoint) -> None:
    """Запрещает смену identity после начала изменения профиля Avito."""

    if endpoint is None:
        return
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    if endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
        MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        MarketplaceFeedEndpoint.ProfileState.VERIFIED,
    }:
        raise MarketplaceAccountFeedConflict(
            'Нельзя изменить подключение, пока stable feed endpoint '
            'используется или сверяется с профилем площадки.',
        )


def _assert_feed_endpoint_availability_mutation_safe(
    endpoint,
    *,
    destructive: bool = False,
) -> None:
    """Защищает доступность аккаунта около изменения профиля Avito."""

    if endpoint is None:
        return
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    if endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    } or (
        destructive
        and endpoint.profile_state in {
            MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        }
    ):
        raise MarketplaceAccountFeedConflict(
            'Нельзя отключить аккаунт во время миграции feed-профиля. '
            'Сначала завершите или сверите миграцию.',
        )


def _fence_marketplace_feed_endpoint_identity(account, endpoint) -> None:
    """Отзывает уже заблокированную capability после смены credentials."""

    if endpoint is None:
        return
    from apps.marketplaces.feed_workflow import account_identity_digest
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    max_revision = (1 << 63) - 1
    if (
        endpoint.capability_revision >= max_revision
        or endpoint.profile_revision >= max_revision
    ):
        raise OverflowError('Marketplace feed endpoint revision is exhausted.')

    endpoint.owner_identity_digest = account_identity_digest(account)
    endpoint.capability_revision += 1
    endpoint.previous_token_key_id = ''
    endpoint.serve_enabled = False
    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW
    endpoint.profile_fingerprint = ''
    endpoint.profile_verified_at = None
    endpoint.profile_revision += 1
    endpoint.save(update_fields=(
        'owner_identity_digest',
        'capability_revision',
        'previous_token_key_id',
        'serve_enabled',
        'profile_state',
        'profile_fingerprint',
        'profile_verified_at',
        'profile_revision',
        'updated_at',
    ))


def _invalidate_avito_access_token_after_commit(account) -> None:
    """Удаляет старый OAuth token только после успешного commit credentials."""

    from apps.marketplaces.adapters.avito.auth import AvitoAuthManager

    transaction.on_commit(partial(AvitoAuthManager().invalidate, account))


class BulkActionItem(TypedDict):
    id: int
    status: str
    message: str


class BulkActionResult(TypedDict):
    total: int
    success: int
    skipped: int
    errors: int
    items: list[BulkActionItem]


class ListingService:
    """Сервис управления объявлениями: создание, маршрутизация по типу изменения."""

    @staticmethod
    def get_for_tenant(listing_id: int, tenant) -> Listing:
        """Возвращает листинг тенанта или бросает ListingNotFound."""
        try:
            return (
                Listing.objects
                .select_related(
                    'product',
                    'product__catalog_category',
                    'account',
                    'placement_address',
                    'bulk_placement_address',
                )
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
        from apps.marketplaces.adapters.avito.feed_builder import unknown_brand_details
        if unknown_brand_details(listing) is not None:
            raise InvalidListingStatus(
                'Неизвестный бренд нельзя отправить в Avito. Выберите значение из '
                'справочника Avito или запросите добавление бренда в поддержке Avito.'
            )
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_QUEUED
        applied = _save_local_listing_intent(
            listing,
            ('status',),
            **expected,
        )

        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_publish_or_update(lid, is_new=True))
        return listing

    @staticmethod
    def publish(listing_id: int, tenant) -> Listing:
        """
        Публикует черновик, отклонённый, архивный или упёршийся в лимит листинг на Avito.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в подходящем статусе для публикации.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        publishable = (
            Listing.STATUS_DRAFT,
            Listing.STATUS_REJECTED,
            Listing.STATUS_ARCHIVED,
            # «Лимит достигнут» — после продления подписки/апгрейда плана
            # листинг должен публиковаться повторно, иначе статус тупиковый.
            Listing.STATUS_LIMIT_REACHED,
        )
        if listing.status not in publishable:
            raise InvalidListingStatus(
                f'Публикация доступна для draft/rejected/archived/limit_reached, '
                f'текущий статус: {listing.status}'
            )
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_QUEUED
        # Сбрасываем причину прошлого отклонения, чтобы старый текст не висел
        # на карточке, пока идёт новая публикация.
        listing.rejection_reason = ''
        applied = _save_local_listing_intent(
            listing,
            ('status', 'rejection_reason'),
            **expected,
        )
        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_publish_or_update(lid, is_new=True))
        return listing

    @staticmethod
    def archive(listing_id: int, tenant) -> Listing:
        """Снимает листинг с публикации через удаление из фида Avito."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status in (Listing.STATUS_ARCHIVING, Listing.STATUS_ARCHIVED, Listing.STATUS_DELETED):
            raise InvalidListingStatus(f'Листинг уже в статусе {listing.status}')
        # Честный статус: «Снимается» — переключим в «В архиве» только после
        # подтверждения снятия от Avito (autoload пакетный, не мгновенный).
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_ARCHIVING
        applied = _save_local_listing_intent(
            listing,
            ('status',),
            **expected,
        )
        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_unpublish(lid))
        return listing

    @staticmethod
    def delete(listing_id: int, tenant) -> Listing:
        """Удаляет листинг локально и отправляет Remove в feed, если есть external_id."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status == Listing.STATUS_DELETED:
            raise InvalidListingStatus('Листинг уже удалён')
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_DELETED
        applied = _save_local_listing_intent(
            listing,
            ('status',),
            **expected,
        )
        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_delete(lid))
        return listing

    @staticmethod
    def check_avito_status(listing_id: int, tenant) -> Listing:
        """Ставит ручную проверку статуса feed/модерации Avito для аккаунта листинга."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status != Listing.STATUS_PENDING:
            raise InvalidListingStatus('Проверка Avito доступна только для объявлений на модерации Avito')
        account_id = listing.account_id
        transaction.on_commit(lambda: _enqueue_poll_feed_results(account_id))
        return listing

    @staticmethod
    def request_regenerate(
        listing_id: int,
        tenant,
        *,
        durable_deduplication_key: str | None = None,
    ) -> Listing:
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
        if durable_deduplication_key:
            from apps.products.models import Product
            from apps.products.services import ProductService

            product = Product.objects.select_related('tenant').get(pk=product_id)
            submission = ProductService.schedule_ai_generation(
                product,
                tenant,
                deduplication_key=durable_deduplication_key,
            )
            # The API transaction persists this exact durable result on its
            # paid-ingress intent.  Keeping it transient avoids changing the
            # public Listing model solely for response plumbing.
            listing.__dict__['_regeneration_submission'] = submission
        else:
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
        expected = _listing_expected_state(listing)
        update_fields = []
        if title is not None:
            listing.title = title[:300]
            update_fields.append('title')
        if description_ai is not None:
            listing.description_ai = description_ai
            update_fields.append('description_ai')
        if update_fields:
            _save_local_listing_intent(
                listing,
                update_fields,
                **expected,
            )
        return listing

    @staticmethod
    def update_listing_fields(listing_id: int, tenant, data: dict) -> Listing:
        """Обновляет аккаунт и цену листинга с tenant-safe проверками."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        active_price_only = (
            listing.status == Listing.STATUS_ACTIVE
            and bool(data)
            and set(data) <= {'price_on_listing', 'margin_pct'}
        )
        if listing.status == Listing.STATUS_DELETED or (
            listing.status == Listing.STATUS_ACTIVE and not active_price_only
        ):
            raise InvalidListingStatus(f'Нельзя редактировать листинг в статусе {listing.status}')

        expected = _listing_expected_state(listing)
        update_fields = []
        account_changed = False
        if 'account_id' in data:
            from apps.marketplaces.models import MarketplaceAccount
            try:
                account = MarketplaceAccount.objects.get(
                    pk=data['account_id'],
                    tenant=tenant,
                    is_active=True,
                )
            except MarketplaceAccount.DoesNotExist:
                raise ListingNotFound('Аккаунт Avito не найден')
            exists = Listing.objects.filter(
                tenant=tenant,
                product=listing.product,
                account=account,
            ).exclude(pk=listing.pk).exists()
            if exists:
                raise ListingAccountConflict('Для этого товара уже есть листинг на выбранном аккаунте')
            account_changed = account.pk != listing.account_id
            listing.account = account
            update_fields.append('account')
            if listing.placement_address and listing.placement_address.account_id != account.pk:
                listing.placement_address = None
                update_fields.append('placement_address')

        if 'margin_pct' in data:
            listing.margin_pct = data['margin_pct']
            update_fields.append('margin_pct')
            # Пересчитываем цену от базовой цены товара
            listing.price_on_listing = compute_price(listing.product.price, effective_margin(listing))
            update_fields.append('price_on_listing')
        elif 'price_on_listing' in data:
            listing.price_on_listing = data['price_on_listing']
            update_fields.append('price_on_listing')

        if 'ad_type' in data:
            listing.ad_type = data['ad_type']
            update_fields.append('ad_type')

        if update_fields:
            applied = _save_local_listing_intent(
                listing,
                update_fields,
                reset_provider_identity=account_changed,
                require_target_account_active=account_changed,
                **expected,
            )
            if active_price_only and applied:
                transaction.on_commit(lambda: _enqueue_price_update(listing.pk))
        return listing

    @staticmethod
    def update_placement(listing_id: int, tenant, data: dict) -> Listing:
        """Обновляет адресные override-поля листинга."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        # Частая ошибка: в поле «ID адреса Avito» вводят external_id аккаунта
        # (он же виден в UI как «ID аккаунта»). Avito такой адрес не находит —
        # отклоняем сразу с понятным пояснением, а не после провала публикации.
        seller_address_id = str(data.get('seller_address_id_override') or '').strip()
        if seller_address_id and seller_address_id == (listing.account.external_id or ''):
            raise InvalidListingStatus(
                'В поле «ID адреса Avito» указан ID аккаунта, а не ID адреса размещения. '
                'Выберите адрес из справочника или укажите корректный ID адреса из профиля Avito.'
            )
        expected = _listing_expected_state(listing)
        update_fields = []
        for field in (
            'address_override',
            'seller_address_id_override',
            'manager_name_override',
            'contact_phone_override',
        ):
            if field in data:
                setattr(listing, field, str(data[field] or '').strip())
                update_fields.append(field)
        if 'placement_address' in data:
            listing.placement_address = _get_placement_address(
                tenant,
                listing.account,
                data.get('placement_address'),
            )
            update_fields.append('placement_address')
        if update_fields:
            _save_local_listing_intent(
                listing,
                update_fields,
                **expected,
            )
        return listing

    @staticmethod
    def bulk_update_placement(tenant, filters: dict, data: dict) -> int:
        """Массово обновляет адресные поля листингов тенанта ниже ручных override."""
        qs = Listing.objects.filter(tenant=tenant)
        listing_ids = filters.get('listing_ids')
        if listing_ids:
            qs = qs.filter(pk__in=listing_ids)
        if filters.get('account_id'):
            qs = qs.filter(account_id=filters['account_id'])
        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        if filters.get('category_source'):
            qs = qs.filter(product__category_1c=filters['category_source'])
        if filters.get('catalog_category_id'):
            qs = qs.filter(product__catalog_category_id=filters['catalog_category_id'])

        field_map = {
            'address_override': 'bulk_address',
            'seller_address_id_override': 'bulk_seller_address_id',
            'manager_name_override': 'bulk_manager_name',
            'contact_phone_override': 'bulk_contact_phone',
        }
        updates: dict[str, Any] = {}
        for input_field, model_field in field_map.items():
            if input_field in data:
                updates[model_field] = str(data[input_field] or '').strip()
        if 'placement_address' in data:
            address_id = data.get('placement_address')
            if address_id:
                from apps.marketplaces.models import MarketplacePlacementAddress
                try:
                    address = MarketplacePlacementAddress.objects.get(pk=address_id, tenant=tenant, is_active=True)
                except MarketplacePlacementAddress.DoesNotExist:
                    raise ListingNotFound('Адрес размещения не найден')
                qs = qs.filter(account=address.account)
                updates['bulk_placement_address'] = address
            else:
                updates['bulk_placement_address'] = None
        if not updates:
            return 0
        target_ids = list(
            qs.order_by('pk').values_list('pk', flat=True)[:settings.API_BULK_MAX_ITEMS + 1],
        )
        if len(target_ids) > settings.API_BULK_MAX_ITEMS:
            raise ListingBulkLimitExceeded(
                f'Массовая операция допускает не более {settings.API_BULK_MAX_ITEMS} листингов.',
            )
        target_qs = qs.filter(pk__in=target_ids)
        if not _status_lifecycle_dual_write_enabled():
            return target_qs.update(**updates)

        account_ids = sorted(set(
            target_qs.values_list('account_id', flat=True),
        ))
        with transaction.atomic():
            locked_accounts = list(
                MarketplaceAccount.all_objects.select_for_update(of=('self',))
                .filter(pk__in=account_ids)
                .order_by('pk')
                .only('pk')
            )
            locked_account_ids = {account.pk for account in locked_accounts}
            rows = list(
                target_qs.filter(account_id__in=locked_account_ids)
                .select_for_update(of=('self',))
                .order_by('pk')
                .values('pk', 'account_id', 'status', 'external_id')
            )
            locked_ids = [row['pk'] for row in rows]
            locked_qs = Listing.objects.filter(pk__in=locked_ids)
            inactive_lifecycle = release_status_check(
                next_status_check_at=None,
            ).as_update_kwargs()
            updated = locked_qs.update(**updates, **inactive_lifecycle)

            short_due = timezone.now() + _LOCAL_STATUS_RECHECK_DELAY
            due_ids = [
                row['pk']
                for row in rows
                if row['status'] == Listing.STATUS_ACTIVE or (
                    row['status'] == Listing.STATUS_ARCHIVING
                    and bool(row['external_id'])
                )
            ]
            if due_ids:
                due_id_set = set(due_ids)
                locked_qs.filter(pk__in=due_ids).update(
                    **release_status_check(
                        next_status_check_at=short_due,
                    ).as_update_kwargs(),
                )
                due_accounts = {
                    row['account_id'] for row in rows if row['pk'] in due_id_set
                }
                for account_id in due_accounts:
                    _min_nudge_account_status_due(account_id, short_due)
            return updated

    @staticmethod
    def bulk_action(tenant, data: dict) -> BulkActionResult:
        """Выполняет массовое действие над tenant-scoped листингами."""
        action = data['action']
        listings = list(
            ListingService._bulk_queryset(tenant, data)
            .select_related('tenant', 'product', 'account')
            .order_by('pk')[:settings.API_BULK_MAX_ITEMS + 1]
        )
        if len(listings) > settings.API_BULK_MAX_ITEMS:
            raise ListingBulkLimitExceeded(
                f'Массовая операция допускает не более {settings.API_BULK_MAX_ITEMS} листингов.',
            )
        result: BulkActionResult = {
            'total': len(listings),
            'success': 0,
            'skipped': 0,
            'errors': 0,
            'items': [],
        }

        for listing in listings:
            try:
                if action == 'publish':
                    ListingService.publish(listing.pk, tenant)
                    message = 'Публикация поставлена в очередь'
                elif action == 'archive':
                    ListingService.archive(listing.pk, tenant)
                    message = 'Снятие с публикации поставлено в очередь'
                elif action == 'delete':
                    ListingService.delete(listing.pk, tenant)
                    message = 'Удаление поставлено в очередь'
                elif action == 'update_placement':
                    ListingService.update_placement(listing.pk, tenant, data)
                    message = 'Адрес размещения обновлён'
                else:
                    raise InvalidListingStatus(f'Неизвестное действие: {action}')
            except InvalidListingStatus as exc:
                result['skipped'] += 1
                result['items'].append({
                    'id': listing.pk,
                    'status': 'skipped',
                    'message': str(exc),
                })
                continue
            except ListingNotFound as exc:
                result['errors'] += 1
                result['items'].append({
                    'id': listing.pk,
                    'status': 'error',
                    'message': str(exc),
                })
                continue

            result['success'] += 1
            result['items'].append({
                'id': listing.pk,
                'status': 'ok',
                'message': message,
            })
            ListingService._write_bulk_item_log(tenant, listing, action, message)

        ListingService._write_bulk_log(tenant, action, result)
        return result

    @staticmethod
    def _bulk_queryset(tenant, filters: dict):
        """Возвращает queryset листингов для массового действия с tenant isolation."""
        qs = Listing.objects.filter(tenant=tenant)
        listing_ids = filters.get('listing_ids')
        if listing_ids:
            qs = qs.filter(pk__in=listing_ids)
        if filters.get('account_id'):
            qs = qs.filter(account_id=filters['account_id'])
        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        return qs

    @staticmethod
    def _write_bulk_log(
        tenant,
        action: str,
        result: BulkActionResult,
    ) -> None:
        """Пишет общий SyncLog по массовому действию."""
        try:
            from apps.sync.models import SyncLog
            status = SyncLog.STATUS_OK if result['errors'] == 0 else SyncLog.STATUS_WARN
            SyncLog.objects.create(
                tenant=tenant,
                event_type=SyncLog.EVENT_LISTING_UPDATE,
                status=status,
                message=(
                    f'Массовое действие listing.{action}: '
                    f'ok={result["success"]}, skipped={result["skipped"]}, errors={result["errors"]}'
                ),
                payload={
                    'action': action,
                    'total': result['total'],
                    'success': result['success'],
                    'skipped': result['skipped'],
                    'errors': result['errors'],
                },
            )
        except Exception:
            pass

    @staticmethod
    def _write_bulk_item_log(tenant, listing: Listing, action: str, message: str) -> None:
        """Пишет SyncLog по конкретному листингу в массовой операции."""
        try:
            from apps.sync.models import SyncLog
            event_map = {
                'publish': SyncLog.EVENT_LISTING_PUBLISH,
                'archive': SyncLog.EVENT_LISTING_UNPUBLISH,
                'delete': SyncLog.EVENT_LISTING_DELETE,
                'update_placement': SyncLog.EVENT_LISTING_UPDATE,
            }
            SyncLog.objects.create(
                tenant=tenant,
                listing=listing,
                product=listing.product,
                event_type=event_map.get(action, SyncLog.EVENT_LISTING_UPDATE),
                status=SyncLog.STATUS_OK,
                message=f'Массовое действие: {message}',
                payload={'action': action},
            )
        except Exception:
            pass

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
            lid = int(listing.pk)
            if not _status_lifecycle_dual_write_enabled():
                transaction.on_commit(partial(_enqueue_unpublish, lid))
                count += 1
                continue
            expected = _listing_expected_state(listing)
            listing.status = Listing.STATUS_ARCHIVING
            if _save_local_listing_intent(
                listing,
                ('status',),
                **expected,
            ):
                transaction.on_commit(partial(_enqueue_unpublish, lid))
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
            # Со страницы товаров создаём ЧЕРНОВИК, а не публикуем сразу —
            # тенант сначала редактирует цену/контакты и публикует вручную.
            listing = ListingService.create_or_update(product, account, auto_publish=False)
            listing_ids.append(listing.pk)
        return listing_ids

    @staticmethod
    def create_or_update(product, account, change_type: str = 'content', auto_publish: bool = True) -> Listing:
        """
        Создаёт листинг или обновляет существующий в зависимости от change_type.

        price_only → только обновить цену (минимальный запрос к Avito).
        Иначе → полное обновление или первичная публикация.
        auto_publish=False → только создать/обновить черновик, без отправки в Avito
        (тенант публикует вручную из вкладки «Листинги»).
        Задача в Celery ставится через transaction.on_commit — не раньше коммита.
        """
        cat = getattr(product, 'catalog_category', None)
        cat_margin = effective_category_margin(cat) if cat else Decimal('0')
        default_price = compute_price(product.price, cat_margin)

        listing = Listing.all_objects.filter(
            tenant=product.tenant, product=product, account=account,
        ).first()
        if listing is None:
            listing = Listing.objects.create(
                tenant=product.tenant,
                product=product,
                account=account,
                price_on_listing=default_price,
                title=(product.title_ai or product.name)[:300],
                description_ai=product.description_ai,
                status=Listing.STATUS_DRAFT,
            )
        else:
            expected = _listing_expected_state(listing)
            new_price = compute_price(product.price, effective_margin(listing))
            update_fields = ['price_on_listing']
            reset_provider_identity = listing.deleted_at is not None
            if reset_provider_identity:
                listing.deleted_at = None
                listing.status = Listing.STATUS_DRAFT
                update_fields.extend(['deleted_at', 'status', 'updated_at'])
            listing.price_on_listing = new_price
            applied = _save_local_listing_intent(
                listing,
                update_fields,
                reset_provider_identity=reset_provider_identity,
                **expected,
            )
            if auto_publish and applied:
                if change_type == 'price_only':
                    transaction.on_commit(lambda: _enqueue_price_update(listing.pk))
                else:
                    transaction.on_commit(
                        lambda: _enqueue_publish_or_update(listing.pk, False),
                    )
            return listing

        if auto_publish:
            transaction.on_commit(lambda: _enqueue_publish_or_update(listing.pk, True))
        return listing


class MarketplaceAccountService:
    """Сервис управления аккаунтами маркетплейсов: создание, обновление credentials."""

    @staticmethod
    def _fetch_avito_user_id(credentials_enc: bytes) -> str:
        """Получает числовой user_id из Avito API по credentials."""
        import requests as req
        from apps.core.http_responses import bounded_http_request
        from apps.marketplaces.adapters.avito.auth import AvitoAuthManager

        class _Tmp:
            pk: None = None
            credentials_enc: bytes

        tmp = _Tmp()
        tmp.credentials_enc = credentials_enc
        try:
            token = AvitoAuthManager()._refresh(tmp)
            resp = bounded_http_request(
                req.get,
                'https://api.avito.ru/core/v1/accounts/self',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
                max_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
            )
            resp.raise_for_status()
            user_id = resp.json().get('id')
        except Exception:
            raise InvalidMarketplaceCredentials('Не удалось проверить Avito API-ключи. Проверьте их правильность.')
        if not user_id:
            raise InvalidMarketplaceCredentials('Avito API не вернул user_id аккаунта')
        return str(user_id)

    @staticmethod
    def create(tenant, data: dict):
        """
        Создаёт аккаунт маркетплейса с зашифрованными credentials.

        Автоматически запрашивает реальный Avito user_id через API.
        При конфликте external_id бросает AccountAlreadyExists.
        """
        from apps.marketplaces.models import MarketplaceAccount

        if (
            data.get('marketplace') == MarketplaceAccount.MARKETPLACE_AVITO
            and getattr(
                settings,
                'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED',
                False,
            )
        ):
            raise MarketplaceAccountFeedConflict(
                'Подключение новых Avito-аккаунтов временно приостановлено '
                'на время миграции Autoload-профилей.',
            )
        from apps.datasources.encryption import encrypt
        credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        external_id = MarketplaceAccountService._fetch_avito_user_id(credentials_enc)
        deleted_account = MarketplaceAccount.all_objects.filter(
            tenant=tenant,
            marketplace=data['marketplace'],
            external_id=external_id,
            deleted_at__isnull=False,
        ).first()
        if deleted_account is not None:
            with transaction.atomic():
                account = (
                    MarketplaceAccount.all_objects.select_for_update()
                    .get(pk=deleted_account.pk)
                )
                feed_endpoint = _lock_marketplace_feed_endpoint(account.pk)
                _assert_feed_endpoint_identity_mutation_safe(feed_endpoint)
                _assert_account_identity_mutation_safe(account)
                account.name = data['name']
                account.credentials_enc = credentials_enc
                account.is_active = True
                account.deleted_at = None
                account.save(update_fields=(
                    'name', 'credentials_enc', 'is_active', 'deleted_at',
                    'updated_at',
                ))
                _fence_marketplace_feed_endpoint_identity(account, feed_endpoint)
                _invalidate_avito_access_token_after_commit(account)
        else:
            try:
                account = MarketplaceAccount.objects.create(
                    tenant=tenant,
                    name=data['name'],
                    marketplace=data['marketplace'],
                    external_id=external_id,
                    credentials_enc=credentials_enc,
                )
            except IntegrityError:
                raise AccountAlreadyExists('Аккаунт с таким external_id уже существует')

        # Регистрируем feed URL в Avito Autoload после коммита транзакции
        if account.marketplace == MarketplaceAccount.MARKETPLACE_AVITO:
            from apps.marketplaces.tasks import setup_autoload_profile_task
            transaction.on_commit(
                lambda: setup_autoload_profile_task.delay(
                    account.pk, account.tenant_id,
                )
            )

        return account

    @staticmethod
    def update_credentials(account, data: dict):
        """Полностью обновляет аккаунт: имя, marketplace, external_id и credentials."""
        from apps.datasources.encryption import encrypt
        credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        external_id = MarketplaceAccountService._fetch_avito_user_id(credentials_enc)
        try:
            with transaction.atomic():
                account = (
                    type(account).all_objects.select_for_update()
                    .get(pk=account.pk)
                )
                feed_endpoint = _lock_marketplace_feed_endpoint(account.pk)
                _assert_feed_endpoint_identity_mutation_safe(feed_endpoint)
                _assert_account_identity_mutation_safe(account)
                account.name = data['name']
                account.marketplace = data['marketplace']
                account.external_id = external_id
                account.credentials_enc = credentials_enc
                account.save(update_fields=(
                    'name', 'marketplace', 'external_id', 'credentials_enc',
                    'updated_at',
                ))
                _fence_marketplace_feed_endpoint_identity(account, feed_endpoint)
                _invalidate_avito_access_token_after_commit(account)
        except IntegrityError:
            raise AccountAlreadyExists('Аккаунт с таким external_id уже существует')
        return account

    @staticmethod
    def update_partial(account, data: dict):
        """Частично обновляет аккаунт: is_active, name и настройки размещения."""
        with transaction.atomic():
            account = (
                type(account).all_objects.select_for_update()
                .get(pk=account.pk)
            )
            update_fields = []
            was_active = account.is_active
            if 'is_active' in data:
                account.is_active = bool(data['is_active'])
                update_fields.append('is_active')
            if 'name' in data:
                account.name = str(data['name'])[:200]
                update_fields.append('name')
            for field in (
                'default_address',
                'default_seller_address_id',
                'default_manager_name',
                'default_contact_phone',
            ):
                if field in data:
                    setattr(account, field, str(data[field] or '').strip())
                    update_fields.append(field)
            if 'autoload_subscription_ends_at' in data:
                account.autoload_subscription_ends_at = data['autoload_subscription_ends_at']
                update_fields.append('autoload_subscription_ends_at')
            if was_active and not account.is_active:
                endpoint = _lock_marketplace_feed_endpoint(account.pk)
                _assert_feed_endpoint_availability_mutation_safe(endpoint)
            if update_fields:
                account.save(update_fields=(*update_fields, 'updated_at'))
        return account


class AvitoAccountStatusService:
    """Синхронизирует подтверждённое состояние профиля и тарифа Avito."""

    @staticmethod
    def _timestamp(value):
        """Преобразует Unix timestamp Avito в timezone-aware datetime."""
        if value in (None, ''):
            return None
        try:
            return datetime.datetime.fromtimestamp(
                int(value), tz=datetime.timezone.utc,
            )
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _price(contract: dict):
        """Возвращает стоимость тарифа как Decimal либо None."""
        value = (contract.get('price') or {}).get('price')
        if value in (None, ''):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _packages(contract: dict) -> list[dict]:
        """Оставляет только безопасные tenant-facing поля пакетов размещений."""
        result = []
        for package in contract.get('packages') or []:
            if not isinstance(package, dict):
                continue
            result.append({
                'categories': package.get('categories') or [],
                'locations': package.get('locations') or [],
                'remain': package.get('remain'),
                'total': package.get('total'),
            })
        return result

    @classmethod
    def _apply_tariff(cls, status_obj: AvitoAccountStatus, payload: dict, checked_at) -> None:
        """Сохраняет нормализованный текущий и следующий тариф."""
        current = payload.get('current') or {}
        if current:
            status_obj.tariff_status = (
                AvitoAccountStatus.TARIFF_ACTIVE
                if current.get('isActive')
                else AvitoAccountStatus.TARIFF_INACTIVE
            )
            status_obj.tariff_name = str(current.get('level') or '')[:200]
            status_obj.tariff_started_at = cls._timestamp(current.get('startTime'))
            status_obj.tariff_ends_at = cls._timestamp(current.get('closeTime'))
            status_obj.tariff_price = cls._price(current)
            status_obj.placement_packages = cls._packages(current)
        else:
            status_obj.tariff_status = AvitoAccountStatus.TARIFF_NOT_FOUND
            status_obj.tariff_name = ''
            status_obj.tariff_started_at = None
            status_obj.tariff_ends_at = None
            status_obj.tariff_price = None
            status_obj.placement_packages = []

        scheduled = payload.get('scheduled') or {}
        status_obj.scheduled_tariff = {
            'name': str(scheduled.get('level') or '')[:200],
            'starts_at': (
                cls._timestamp(scheduled.get('startTime')).isoformat()
                if cls._timestamp(scheduled.get('startTime'))
                else None
            ),
            'price': (
                str(cls._price(scheduled))
                if cls._price(scheduled) is not None
                else None
            ),
        } if scheduled else {}
        status_obj.tariff_checked_at = checked_at

    @staticmethod
    def _days_left(status_obj: AvitoAccountStatus) -> int | None:
        """Возвращает дни по API-тарифу или по ручной дате Autoload."""
        if status_obj.tariff_ends_at:
            seconds_left = (status_obj.tariff_ends_at - timezone.now()).total_seconds()
            if seconds_left <= 0:
                return 0
            return int((seconds_left + 86399) // 86400)
        manual_end = status_obj.account.autoload_subscription_ends_at
        if not manual_end:
            return None
        return max((manual_end - timezone.localdate()).days, 0)

    @staticmethod
    def _period_key(status_obj: AvitoAccountStatus) -> str:
        if status_obj.tariff_ends_at:
            return f'api:{status_obj.tariff_ends_at.isoformat()}'
        if status_obj.account.autoload_subscription_ends_at:
            return f'manual:{status_obj.account.autoload_subscription_ends_at.isoformat()}'
        return ''

    @staticmethod
    def _queue_notification(status_obj: AvitoAccountStatus, level: str, message: str) -> None:
        """Отправляет уведомление после фиксации снимка в транзакции."""
        from apps.notifications.tasks import send_notification_task

        transaction.on_commit(
            lambda: send_notification_task.delay(
                status_obj.tenant_id, level, message,
                {'account_id': status_obj.account_id},
            )
        )

    @classmethod
    def _notify_thresholds(cls, status_obj: AvitoAccountStatus) -> None:
        """Дедуплицированно уведомляет о сроке, лимите и отключении Autoload."""
        from apps.notifications.services import LEVEL_CRITICAL, LEVEL_ERROR

        state = dict(status_obj.notification_state or {})
        period_key = cls._period_key(status_obj)
        if state.get('period') != period_key:
            state = {'period': period_key}

        if (
            status_obj.connection_status
            == AvitoAccountStatus.CONNECTION_AUTH_ERROR
            and state.get('connection') != AvitoAccountStatus.CONNECTION_AUTH_ERROR
        ):
            cls._queue_notification(
                status_obj,
                LEVEL_CRITICAL,
                f'Avito ({status_obj.account.name}): ключи доступа отклонены. '
                'Переподключите аккаунт.',
            )
            state['connection'] = AvitoAccountStatus.CONNECTION_AUTH_ERROR
        elif status_obj.connection_status == AvitoAccountStatus.CONNECTION_CONNECTED:
            state.pop('connection', None)

        days_left = cls._days_left(status_obj)
        if (
            status_obj.autoload_status == AvitoAccountStatus.AUTOLOAD_ENABLED
            and days_left is not None
        ):
            expiry_threshold = next(
                (threshold for threshold in (0, 1, 3, 7, 14) if days_left <= threshold),
                None,
            )
            if expiry_threshold is not None and state.get('expiry') != expiry_threshold:
                level = LEVEL_CRITICAL if days_left <= 1 else LEVEL_ERROR
                if status_obj.tariff_ends_at:
                    expiry_message = (
                        f'Avito ({status_obj.account.name}): до окончания '
                        f'тарифа осталось {days_left} дн.'
                    )
                else:
                    manual_end = status_obj.account.autoload_subscription_ends_at
                    if manual_end and manual_end < timezone.localdate():
                        expiry_message = (
                            f'Avito ({status_obj.account.name}): указанная вручную '
                            'дата окончания Автозагрузки '
                            f'{manual_end:%d.%m.%Y} уже прошла. '
                            'Проверьте актуальный срок в Avito.'
                        )
                    elif days_left == 0:
                        expiry_message = (
                            f'Avito ({status_obj.account.name}): указанная вручную '
                            'дата окончания Автозагрузки — сегодня.'
                        )
                    else:
                        expiry_message = (
                            f'Avito ({status_obj.account.name}): по указанной вручную '
                            'дате до окончания Автозагрузки '
                            f'осталось {days_left} дн.'
                        )
                cls._queue_notification(
                    status_obj,
                    level,
                    expiry_message,
                )
                state['expiry'] = expiry_threshold

        if (
            status_obj.tariff_status == AvitoAccountStatus.TARIFF_INACTIVE
            and state.get('tariff') != AvitoAccountStatus.TARIFF_INACTIVE
        ):
            cls._queue_notification(
                status_obj,
                LEVEL_CRITICAL,
                f'Avito ({status_obj.account.name}): тариф неактивен.',
            )
            state['tariff'] = AvitoAccountStatus.TARIFF_INACTIVE
        elif status_obj.tariff_status == AvitoAccountStatus.TARIFF_ACTIVE:
            state.pop('tariff', None)

        remaining: list[int] = []
        totals: list[int] = []
        for package in status_obj.placement_packages:
            if not isinstance(package, dict):
                continue
            remain = package.get('remain')
            total = package.get('total')
            if isinstance(remain, int):
                remaining.append(remain)
            if isinstance(total, int):
                totals.append(total)
        if remaining and totals and sum(totals) > 0:
            percent_left = int(sum(remaining) * 100 / sum(totals))
            limit_threshold = next(
                (threshold for threshold in (0, 10, 20) if percent_left <= threshold),
                None,
            )
            if limit_threshold is not None and state.get('placements') != limit_threshold:
                cls._queue_notification(
                    status_obj,
                    LEVEL_CRITICAL if percent_left == 0 else LEVEL_ERROR,
                    f'Avito ({status_obj.account.name}): осталось '
                    f'{sum(remaining)} размещений из {sum(totals)}.',
                )
                state['placements'] = limit_threshold

        if status_obj.autoload_status in {
            AvitoAccountStatus.AUTOLOAD_DISABLED,
            AvitoAccountStatus.AUTOLOAD_MISSING,
            AvitoAccountStatus.AUTOLOAD_FORBIDDEN,
        } and state.get('autoload') != status_obj.autoload_status:
            cls._queue_notification(
                status_obj,
                LEVEL_CRITICAL,
                f'Avito ({status_obj.account.name}): Автозагрузка недоступна '
                f'({dict(AvitoAccountStatus.AUTOLOAD_CHOICES).get(status_obj.autoload_status)}).',
            )
            state['autoload'] = status_obj.autoload_status
        elif status_obj.autoload_status == AvitoAccountStatus.AUTOLOAD_ENABLED:
            state.pop('autoload', None)

        if state != status_obj.notification_state:
            status_obj.notification_state = state
            status_obj.save(update_fields=['notification_state', 'updated_at'])

    @classmethod
    def refresh(cls, account) -> AvitoAccountStatus:
        """
        Обновляет снимок аккаунта, не стирая подтверждённые данные при временном сбое.

        Отсутствие тарифа и профиля — подтверждённые ответы Avito. Таймауты,
        rate limit и 5xx сохраняются только как ошибка последней попытки.
        """
        from requests import RequestException

        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        from apps.marketplaces.adapters.avito.error_handler import (
            ForbiddenError,
            NotFoundError,
            ServerError,
            TokenExpiredError,
        )
        from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError

        status_obj, _ = AvitoAccountStatus.objects.get_or_create(
            tenant=account.tenant,
            account=account,
        )
        checked_at = timezone.now()
        status_obj.last_attempted_at = checked_at
        errors = []
        adapter = AvitoAdapter(account)

        try:
            profile = adapter.get_autoload_profile()
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_CONNECTED
            status_obj.autoload_status = (
                AvitoAccountStatus.AUTOLOAD_ENABLED
                if profile.get('autoload_enabled')
                else AvitoAccountStatus.AUTOLOAD_DISABLED
            )
            expected_feed_url = adapter._feed_public_url()
            status_obj.feed_configured = any(
                feed.get('feed_url') == expected_feed_url
                for feed in (profile.get('feeds_data') or [])
                if isinstance(feed, dict)
            )
            status_obj.profile_checked_at = checked_at
        except NotFoundError:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_CONNECTED
            status_obj.autoload_status = AvitoAccountStatus.AUTOLOAD_MISSING
            status_obj.feed_configured = False
            status_obj.profile_checked_at = checked_at
        except ForbiddenError:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_CONNECTED
            status_obj.autoload_status = AvitoAccountStatus.AUTOLOAD_FORBIDDEN
            status_obj.feed_configured = None
            status_obj.profile_checked_at = checked_at
        except TokenExpiredError:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_AUTH_ERROR
            errors.append(('auth_error', 'Avito отклонил ключи доступа'))
        except (RateLimitError, ServerError, RequestException):
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_UNAVAILABLE
            errors.append(('profile_unavailable', 'Не удалось обновить профиль Автозагрузки'))
        except Exception:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_UNAVAILABLE
            errors.append(('profile_unavailable', 'Не удалось обновить профиль Автозагрузки'))

        if status_obj.connection_status != AvitoAccountStatus.CONNECTION_AUTH_ERROR:
            try:
                cls._apply_tariff(status_obj, adapter.get_tariff_info(), checked_at)
            except NotFoundError:
                cls._apply_tariff(status_obj, {}, checked_at)
            except ForbiddenError:
                cls._apply_tariff(status_obj, {}, checked_at)
            except TokenExpiredError:
                status_obj.connection_status = AvitoAccountStatus.CONNECTION_AUTH_ERROR
                errors.append(('auth_error', 'Avito отклонил ключи доступа'))
            except (RateLimitError, ServerError, RequestException):
                errors.append(('tariff_unavailable', 'Не удалось обновить тариф Avito'))
            except Exception:
                errors.append(('tariff_unavailable', 'Не удалось обновить тариф Avito'))

        if errors:
            status_obj.last_error_code, status_obj.last_error_message = errors[-1]
        else:
            status_obj.last_error_code = ''
            status_obj.last_error_message = ''
        status_obj.save()

        account.autoload_active = {
            AvitoAccountStatus.AUTOLOAD_ENABLED: True,
            AvitoAccountStatus.AUTOLOAD_DISABLED: False,
            AvitoAccountStatus.AUTOLOAD_MISSING: False,
            AvitoAccountStatus.AUTOLOAD_FORBIDDEN: False,
        }.get(status_obj.autoload_status)
        if status_obj.profile_checked_at:
            account.autoload_checked_at = status_obj.profile_checked_at
        account.save(update_fields=['autoload_active', 'autoload_checked_at'])
        cls._notify_thresholds(status_obj)
        return status_obj


def _enqueue_publish_or_update(listing_id: int, is_new: bool) -> None:
    """Ставит задачу публикации или обновления листинга в Celery."""
    from apps.marketplaces.tasks import publish_listing_task, update_listing_task
    if is_new:
        publish_listing_task.delay(listing_id)
    else:
        update_listing_task.delay(listing_id)


class StatsService:
    """Сервис получения и сохранения ежедневной статистики листингов с Avito."""

    @staticmethod
    def _fetch_raw(account, item_ids: list[str], date_from: datetime.date, date_to: datetime.date) -> list[dict]:
        """
        Вызывает AvitoAdapter.get_stats и возвращает сырой ответ API.

        Вынесен отдельным методом для удобного mock-а в тестах.
        """
        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        return AvitoAdapter(account).get_stats(item_ids, date_from, date_to)

    @classmethod
    def fetch_for_account(
        cls,
        account,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> int:
        """
        Получает статистику активных листингов аккаунта за период и сохраняет в ListingStats.

        Использует bulk_create с update_conflicts — идемпотентен при повторном вызове.
        Возвращает количество обработанных записей (не уникальных: один листинг × N дней).
        """
        listings = list(
            Listing.objects.filter(
                account=account,
                status=Listing.STATUS_ACTIVE,
                external_id__isnull=False,
            ).values('id', 'external_id', 'tenant_id')
        )
        if not listings:
            return 0

        listing_by_external = {item['external_id']: item for item in listings}
        raw = cls._fetch_raw(account, list(listing_by_external.keys()), date_from, date_to)

        to_upsert = []
        for item in raw:
            info = listing_by_external.get(str(item.get('itemId', '')))
            if not info:
                continue
            for day in item.get('stats', []):
                # uniqViews — уникальные просмотры карточки (views в нашей модели)
                # views — все просмотры, используем как прокси для показов (impressions)
                # uniqContacts — уникальные контакты (contacts в нашей модели)
                views = int(day.get('uniqViews', 0) or 0)
                impressions = int(day.get('views', 0) or 0)
                contacts = int(day.get('uniqContacts', 0) or 0)
                ctr = round(views / impressions * 100, 2) if impressions else 0.0
                to_upsert.append(ListingStats(
                    listing_id=info['id'],
                    tenant_id=info['tenant_id'],
                    date=day['date'],
                    views=views,
                    impressions=impressions,
                    contacts=contacts,
                    ctr=ctr,
                ))

        if not to_upsert:
            return 0

        ListingStats.objects.bulk_create(
            to_upsert,
            update_conflicts=True,
            unique_fields=['listing_id', 'date'],
            update_fields=['views', 'impressions', 'contacts', 'ctr'],
        )
        return len(to_upsert)


def _get_placement_address(tenant, account, address_id):
    """Возвращает активный адрес tenant-а для конкретного аккаунта или None."""
    if not address_id:
        return None
    from apps.marketplaces.models import MarketplacePlacementAddress
    try:
        return MarketplacePlacementAddress.objects.get(
            pk=address_id,
            tenant=tenant,
            account=account,
            is_active=True,
        )
    except MarketplacePlacementAddress.DoesNotExist:
        raise ListingNotFound('Адрес размещения не найден')


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


def _enqueue_delete(listing_id: int) -> None:
    """Ставит задачу удаления листинга в Celery."""
    from apps.marketplaces.tasks import delete_listing_task
    delete_listing_task.delay(listing_id)


def _enqueue_poll_feed_results(account_id: int) -> None:
    """Ставит ручную проверку результатов Avito feed в Celery."""
    from apps.marketplaces.tasks import poll_feed_results_task
    poll_feed_results_task.delay(account_id)
