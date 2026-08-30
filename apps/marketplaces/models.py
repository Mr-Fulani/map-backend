import uuid
from datetime import timedelta

from django.db import models, transaction

from apps.core.models import SoftDeleteModel, SoftDeleteQuerySet, TimestampedModel
from apps.tenants.models import Tenant


_MARKETPLACE_FEED_ACTIVE_STATES = (
    'preparing',
    'submit_unknown',
    'polling',
    'reporting',
    'retry_wait',
)
_MARKETPLACE_FEED_OWNERSHIP_STATES = (
    *_MARKETPLACE_FEED_ACTIVE_STATES,
    'outcome_uncertain',
)
_MARKETPLACE_FEED_TERMINAL_STATES = (
    'succeeded',
    'failed',
    'outcome_uncertain',
    'superseded',
    'cancelled',
)


class MarketplaceAccountQuerySet(SoftDeleteQuerySet):
    """Require an explicit, fenced path for marketplace-account removal."""

    def delete(self):
        from django.db.models.deletion import ProtectedError

        raise ProtectedError(
            'MarketplaceAccount bulk deletion is disabled; use the fenced '
            'instance/service path or the explicit retention hard-delete path.',
            set(),
        )


MarketplaceAccountManagerBase = models.Manager.from_queryset(
    MarketplaceAccountQuerySet,
)


class MarketplaceAccountManager(MarketplaceAccountManagerBase):
    """Default manager that hides soft-deleted marketplace accounts."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AvitoCategory(models.Model):
    """Категория Avito из официального справочника."""

    avito_id = models.IntegerField(unique=True, verbose_name='ID категории Avito')
    name = models.CharField(max_length=200, verbose_name='Название')
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='children', verbose_name='Родительская категория',
    )
    is_leaf = models.BooleanField(default=False, verbose_name='Конечная категория')

    class Meta:
        verbose_name = 'Категория Avito'
        verbose_name_plural = 'Категории Avito'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} (id={self.avito_id})'


class AvitoBrandCatalog(models.Model):
    """Последняя успешно проверенная версия справочника Brand из Avito.

    Запись глобальная для платформы (singleton с pk=1): справочник Avito не
    зависит от тенанта. Хранение в БД делает одну версию доступной Django и
    всем Celery-контейнерам и сохраняет её между деплоями.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    source_node = models.CharField(max_length=100)
    field_id = models.PositiveIntegerField()
    brands = models.JSONField(default=list)
    synced_at = models.DateTimeField()

    class Meta:
        verbose_name = 'Справочник брендов Avito'
        verbose_name_plural = 'Справочник брендов Avito'

    def __str__(self):
        return f'Avito Brand: {len(self.brands)} значений ({self.synced_at:%d.%m.%Y})'


class CategoryMapping(TimestampedModel):
    """Маппинг категорий источника данных на категории Avito."""

    MARKETPLACE_AVITO = 'avito'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='category_mappings', verbose_name='Тенант',
    )
    marketplace = models.CharField(max_length=50, default=MARKETPLACE_AVITO, verbose_name='Маркетплейс')
    category_source = models.CharField(max_length=300, verbose_name='Категория источника')
    category_target = models.CharField(max_length=200, verbose_name='Категория Avito')
    category_id = models.IntegerField(verbose_name='ID категории Avito')
    attributes_map = models.JSONField(default=dict, verbose_name='Маппинг атрибутов')
    version = models.PositiveSmallIntegerField(default=1, verbose_name='Версия маппинга')

    class Meta:
        verbose_name = 'Маппинг категорий'
        verbose_name_plural = 'Маппинг категорий'
        unique_together = [('tenant', 'marketplace', 'category_source')]

    def __str__(self):
        return f'{self.tenant.slug}: {self.category_source} → {self.category_target}'


class MarketplaceAccount(SoftDeleteModel):
    """Аккаунт внешнего маркетплейса, принадлежащий одному тенанту."""

    MARKETPLACE_AVITO = 'avito'
    MARKETPLACE_OZON = 'ozon'
    MARKETPLACE_CHOICES = [
        (MARKETPLACE_AVITO, 'Avito'),
        (MARKETPLACE_OZON, 'Ozon'),
    ]

    objects = MarketplaceAccountManager()  # type: ignore[misc, assignment]
    all_objects = MarketplaceAccountManagerBase()  # type: ignore[misc]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='marketplace_accounts', verbose_name='Тенант',
    )
    marketplace = models.CharField(
        max_length=50, choices=MARKETPLACE_CHOICES,
        default=MARKETPLACE_AVITO, verbose_name='Маркетплейс',
    )
    name = models.CharField(max_length=200, verbose_name='Название аккаунта')
    external_id = models.CharField(max_length=100, verbose_name='ID аккаунта у маркетплейса')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    credentials_enc = models.BinaryField(verbose_name='Токены доступа (зашифровано)')
    token_expires_at = models.DateTimeField(null=True, blank=True, verbose_name='Токен истекает')
    requests_this_hour = models.PositiveIntegerField(default=0, verbose_name='Запросов за текущий час')
    hour_bucket_reset_at = models.DateTimeField(null=True, blank=True, verbose_name='Сброс счётчика запросов')
    default_address = models.CharField(max_length=500, blank=True, verbose_name='Адрес по умолчанию')
    default_seller_address_id = models.CharField(
        max_length=100, blank=True, verbose_name='ID адреса продавца Avito по умолчанию',
    )
    default_manager_name = models.CharField(max_length=100, blank=True, verbose_name='Контактное лицо')
    default_contact_phone = models.CharField(max_length=50, blank=True, verbose_name='Контактный телефон')
    # Последний известный статус Avito Автозагрузки. null — ещё не проверяли.
    # При сбое связи статус не понижаем (показываем последнее известное).
    autoload_active = models.BooleanField(null=True, blank=True, verbose_name='Автозагрузка Avito активна')
    autoload_checked_at = models.DateTimeField(null=True, blank=True, verbose_name='Статус Автозагрузки проверен')
    autoload_subscription_ends_at = models.DateField(
        null=True,
        blank=True,
        verbose_name='Дата окончания Автозагрузки',
        help_text=(
            'Заполняется вручную, когда Avito Autoload API не возвращает срок подписки. '
            'Тариф API категории «Транспорт», если доступен, имеет приоритет.'
        ),
    )
    # Когда последний раз триггерили Autoload. Avito читает фид ~раз в час, поэтому
    # изменения копятся в окне и уходят одним фидом (см. request_feed_flush).
    last_feed_flush_at = models.DateTimeField(null=True, blank=True, verbose_name='Последняя автозагрузка фида')
    # Provider-neutral due cursor and short account-batch dispatch lease.
    status_batch_due_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Следующая проверка статусов аккаунта',
    )
    status_batch_cooldown_until = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Проверки статусов приостановлены до',
    )
    status_batch_claim_token = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Токен владельца проверки статусов',
    )
    status_batch_claimed_until = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Lease проверки статусов истекает',
    )
    # Durable desired-state cursor. The fields are additive and remain inert
    # while MARKETPLACE_FEED_INGRESS_MODE=legacy.
    feed_intent_revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Ревизия требуемого состояния фида',
    )
    feed_intent_dispatched_revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Последняя отправленная ревизия фида',
    )
    feed_intent_due_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Следующая отправка ревизии фида',
    )

    class Meta:
        verbose_name = 'Аккаунт маркетплейса'
        verbose_name_plural = 'Аккаунты маркетплейсов'
        unique_together = [('tenant', 'marketplace', 'external_id')]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    feed_intent_dispatched_revision__lte=models.F(
                        'feed_intent_revision',
                    ),
                ),
                name='mkt_acct_intent_order',
            ),
            models.UniqueConstraint(
                fields=['marketplace', 'external_id'],
                condition=models.Q(marketplace='ozon'),
                name='mkt_acct_ozon_identity_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['marketplace', 'status_batch_due_at', 'id'],
                name='mkt_acct_provider_due',
                condition=models.Q(
                    deleted_at__isnull=True,
                    is_active=True,
                    status_batch_due_at__isnull=False,
                ),
            ),
            models.Index(
                fields=['marketplace', 'feed_intent_due_at', 'id'],
                name='mkt_acct_feed_intent_due',
                condition=models.Q(
                    deleted_at__isnull=True,
                    is_active=True,
                    feed_intent_due_at__isnull=False,
                ),
            ),
        ]

    def __str__(self):
        return f'{self.tenant.slug} / {self.name}'

    def soft_delete(self):
        if self.deleted_at is not None:
            return
        from django.utils import timezone

        with transaction.atomic():
            locked = type(self).all_objects.select_for_update().get(pk=self.pk)
            if locked.deleted_at is not None:
                self.deleted_at = locked.deleted_at
                self.is_active = locked.is_active
                return
            from apps.marketplaces.services import (
                _assert_legacy_feed_cursor_mutation_safe,
                _assert_feed_endpoint_availability_mutation_safe,
                _lock_marketplace_feed_endpoint,
            )

            endpoint = _lock_marketplace_feed_endpoint(locked.pk)
            _assert_legacy_feed_cursor_mutation_safe(locked)
            _assert_feed_endpoint_availability_mutation_safe(
                endpoint,
                destructive=True,
            )
            if (
                Listing.all_objects.filter(account_id=locked.pk).filter(
                    models.Q(status__in=[
                        Listing.STATUS_ACTIVE,
                        Listing.STATUS_PENDING,
                        Listing.STATUS_QUEUED,
                        Listing.STATUS_ARCHIVING,
                    ])
                    | (
                        models.Q(external_id__isnull=False)
                        & ~models.Q(external_id='')
                        & ~models.Q(status=Listing.STATUS_ARCHIVED)
                    )
                ).exists()
            ):
                from apps.marketplaces.services import (
                    MarketplaceAccountFeedConflict,
                )

                raise MarketplaceAccountFeedConflict(
                    'Нельзя удалить аккаунт: для него ещё есть '
                    'опубликованные или ожидающие объявления. '
                    'Сначала снимите их с публикации и дождитесь '
                    'подтверждения Avito.',
                )
            from apps.marketplaces.feed_workflow import (
                OWNER_CHANGE_HOLD_SUBMITTED,
                fence_account_feed_runs_for_owner_change,
            )

            fence_account_feed_runs_for_owner_change(
                locked.pk,
                reason='Marketplace account was soft-deleted.',
                safe_state=MarketplaceFeedRun.State.CANCELLED,
                submitted_policy=OWNER_CHANGE_HOLD_SUBMITTED,
            )
            deleted_at = timezone.now()
            list(
                locked.listings.select_for_update()
                .order_by('pk')
                .values_list('pk', flat=True)
            )
            locked.listings.update(
                deleted_at=deleted_at,
                next_status_check_at=None,
                status_check_claim_token=None,
                status_check_claimed_until=None,
            )
            locked.is_active = False
            locked.deleted_at = deleted_at
            locked.status_batch_due_at = None
            locked.status_batch_cooldown_until = None
            locked.status_batch_claim_token = None
            locked.status_batch_claimed_until = None
            locked.save(update_fields=[
                'is_active', 'deleted_at', 'status_batch_due_at',
                'status_batch_cooldown_until', 'status_batch_claim_token',
                'status_batch_claimed_until', 'updated_at',
            ])
            self.is_active = locked.is_active
            self.deleted_at = locked.deleted_at
            self.status_batch_due_at = None
            self.status_batch_cooldown_until = None
            self.status_batch_claim_token = None
            self.status_batch_claimed_until = None


class OzonAccountProfile(TimestampedModel):
    """Проверенный read-only снимок подключения аккаунта Ozon."""

    class ConnectionStatus(models.TextChoices):
        CONNECTED = 'connected', 'Подключён'
        WAREHOUSE_MISSING = 'warehouse_missing', 'Склад не найден'
        WAREHOUSE_SELECTION_REQUIRED = (
            'warehouse_selection_required',
            'Требуется выбрать склад',
        )

    account = models.OneToOneField(
        MarketplaceAccount,
        on_delete=models.CASCADE,
        related_name='ozon_profile',
        verbose_name='Аккаунт Ozon',
    )
    connection_status = models.CharField(
        max_length=40,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.CONNECTED,
        verbose_name='Статус подключения',
    )
    company_name = models.CharField(max_length=300, blank=True, verbose_name='Компания')
    seller_name = models.CharField(max_length=300, blank=True, verbose_name='Продавец')
    currency = models.CharField(max_length=10, blank=True, verbose_name='Валюта')
    roles = models.JSONField(default=list, verbose_name='Роли API-ключа')
    api_methods = models.JSONField(default=list, verbose_name='Методы API-ключа')
    api_key_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='API-ключ истекает',
    )
    warehouse_count = models.PositiveIntegerField(default=0, verbose_name='Количество складов')
    selected_warehouse_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name='Выбранный склад Ozon',
    )
    selected_warehouse_name = models.CharField(
        max_length=300,
        blank=True,
        verbose_name='Название выбранного склада',
    )
    last_checked_at = models.DateTimeField(verbose_name='Подключение проверено')

    class Meta:
        verbose_name = 'Профиль аккаунта Ozon'
        verbose_name_plural = 'Профили аккаунтов Ozon'

    def __str__(self):
        return f'Ozon / {self.account}'


class OzonCategoryTreeSnapshot(TimestampedModel):
    """Immutable Ozon category/type schema revision for one exact account."""

    LANGUAGE_DEFAULT = 'DEFAULT'
    LANGUAGE_CHOICES = [
        (LANGUAGE_DEFAULT, 'По умолчанию'),
        ('RU', 'Русский'),
        ('EN', 'Английский'),
        ('TR', 'Турецкий'),
        ('ZH_HANS', 'Китайский'),
    ]

    account = models.ForeignKey(
        MarketplaceAccount,
        on_delete=models.CASCADE,
        related_name='ozon_category_tree_snapshots',
        verbose_name='Аккаунт Ozon',
    )
    language = models.CharField(
        max_length=10,
        choices=LANGUAGE_CHOICES,
        default=LANGUAGE_DEFAULT,
        verbose_name='Язык схемы',
    )
    schema_hash = models.CharField(
        max_length=64,
        verbose_name='SHA-256 нормализованной схемы',
    )
    tree = models.JSONField(verbose_name='Нормализованное дерево категорий')
    node_count = models.PositiveIntegerField(verbose_name='Количество узлов')
    active_type_count = models.PositiveIntegerField(
        verbose_name='Количество доступных типов товаров',
    )

    class Meta:
        verbose_name = 'Снимок дерева категорий Ozon'
        verbose_name_plural = 'Снимки дерева категорий Ozon'
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'language', 'schema_hash'],
                name='mkt_oz_tree_revision_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['account', 'language', '-updated_at'],
                name='mkt_oz_tree_latest_idx',
            ),
        ]

    def __str__(self):
        return f'Ozon tree {self.account_id} / {self.schema_hash[:12]}'


class OzonCategoryAttributeSnapshot(TimestampedModel):
    """Immutable Ozon attribute schema revision for a category/type pair."""

    account = models.ForeignKey(
        MarketplaceAccount,
        on_delete=models.CASCADE,
        related_name='ozon_category_attribute_snapshots',
        verbose_name='Аккаунт Ozon',
    )
    description_category_id = models.PositiveBigIntegerField(
        verbose_name='ID категории Ozon',
    )
    type_id = models.PositiveBigIntegerField(verbose_name='ID типа товара Ozon')
    language = models.CharField(
        max_length=10,
        choices=OzonCategoryTreeSnapshot.LANGUAGE_CHOICES,
        default=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
        verbose_name='Язык схемы',
    )
    schema_hash = models.CharField(
        max_length=64,
        verbose_name='SHA-256 нормализованной схемы',
    )
    attributes = models.JSONField(verbose_name='Нормализованные характеристики')
    attribute_count = models.PositiveIntegerField(
        verbose_name='Количество характеристик',
    )
    required_attribute_count = models.PositiveIntegerField(
        verbose_name='Количество обязательных характеристик',
    )

    class Meta:
        verbose_name = 'Снимок характеристик категории Ozon'
        verbose_name_plural = 'Снимки характеристик категорий Ozon'
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'account', 'description_category_id', 'type_id',
                    'language', 'schema_hash',
                ],
                name='mkt_oz_attr_revision_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(description_category_id__gt=0),
                name='mkt_oz_attr_category_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(type_id__gt=0),
                name='mkt_oz_attr_type_pos',
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    'account', 'description_category_id', 'type_id',
                    'language', '-updated_at',
                ],
                name='mkt_oz_attr_latest_idx',
            ),
        ]

    def __str__(self):
        return (
            f'Ozon attributes {self.account_id} / '
            f'{self.description_category_id}:{self.type_id}'
        )


def new_ozon_offer_id() -> str:
    return f'map-{uuid.uuid4().hex}'


class OzonOfferDraft(TimestampedModel):
    """Local account-scoped Ozon preparation; never enters Avito Listing."""

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='ozon_offer_drafts',
        verbose_name='Тенант',
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='ozon_offer_drafts',
        verbose_name='Товар',
    )
    account = models.ForeignKey(
        MarketplaceAccount,
        on_delete=models.CASCADE,
        related_name='ozon_offer_drafts',
        verbose_name='Аккаунт Ozon',
    )
    offer_id = models.CharField(
        max_length=100,
        default=new_ozon_offer_id,
        editable=False,
        verbose_name='Стабильный Offer ID',
    )
    description_category_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        verbose_name='ID категории Ozon',
    )
    type_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        verbose_name='ID типа товара Ozon',
    )
    category_path = models.CharField(
        max_length=1000,
        blank=True,
        verbose_name='Путь категории Ozon',
    )
    type_name = models.CharField(
        max_length=500,
        blank=True,
        verbose_name='Тип товара Ozon',
    )
    tree_revision = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = 'Черновик товара Ozon'
        verbose_name_plural = 'Черновики товаров Ozon'
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'product', 'account'],
                name='mkt_oz_offer_product_account_uniq',
            ),
            models.UniqueConstraint(
                fields=['account', 'offer_id'],
                name='mkt_oz_offer_identity_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['tenant', 'account', '-updated_at'],
                name='mkt_oz_offer_tenant_idx',
            ),
        ]

    def __str__(self):
        return f'Ozon offer {self.offer_id} / product {self.product_id}'


class MarketplaceFeedRun(TimestampedModel):
    """Durable, provider-neutral ownership record for one feed generation.

    The UUID is the immutable generation identity stamped onto included
    listings. Mutable progress uses ``revision`` for compare-and-swap
    fencing, so a delayed worker cannot apply an older cursor or report page.
    """

    class State(models.TextChoices):
        PREPARING = 'preparing', 'Подготовка'
        SUBMIT_UNKNOWN = 'submit_unknown', 'Результат отправки неизвестен'
        POLLING = 'polling', 'Ожидание обработки'
        REPORTING = 'reporting', 'Получение отчёта'
        RETRY_WAIT = 'retry_wait', 'Ожидание повтора'
        SUCCEEDED = 'succeeded', 'Завершено'
        FAILED = 'failed', 'Ошибка'
        OUTCOME_UNCERTAIN = 'outcome_uncertain', 'Результат отправки требует сверки'
        SUPERSEDED = 'superseded', 'Заменено новым запуском'
        CANCELLED = 'cancelled', 'Отменено'

    ACTIVE_STATES = _MARKETPLACE_FEED_ACTIVE_STATES
    OWNERSHIP_STATES = _MARKETPLACE_FEED_OWNERSHIP_STATES
    TERMINAL_STATES = _MARKETPLACE_FEED_TERMINAL_STATES

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='marketplace_feed_runs',
        verbose_name='Тенант',
    )
    account = models.ForeignKey(
        MarketplaceAccount,
        on_delete=models.CASCADE,
        related_name='feed_runs',
        verbose_name='Аккаунт маркетплейса',
    )
    marketplace = models.CharField(
        max_length=50,
        editable=False,
        verbose_name='Маркетплейс на момент запуска',
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PREPARING,
        editable=False,
        verbose_name='Состояние',
    )
    revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Ревизия состояния',
    )
    account_identity_digest = models.CharField(
        max_length=64,
        editable=False,
        verbose_name='Отпечаток идентичности аккаунта',
    )
    payload_sha256 = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name='SHA-256 отправленного фида',
    )
    provider_run_id = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        editable=False,
        verbose_name='ID запуска у площадки',
    )
    provider_predecessor_run_id = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        editable=False,
        verbose_name='ID предыдущего запуска у площадки',
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Фид отправлен',
    )
    provider_result_deadline_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Крайний срок сверки результата площадки',
    )
    submission_reconcile_attempt = models.PositiveSmallIntegerField(
        default=0,
        editable=False,
        verbose_name='Подтверждённых отрицательных сверок отправки',
    )
    poll_cursor_listing_id = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Курсор проверки листингов',
    )
    poll_round = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name='Раунд проверки',
    )
    report_page = models.PositiveIntegerField(
        default=1,
        editable=False,
        verbose_name='Следующая страница отчёта',
    )
    report_attempt = models.PositiveSmallIntegerField(
        default=0,
        editable=False,
        verbose_name='Попытка получения отчёта',
    )
    report_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Отчёт площадки полностью обработан',
    )
    next_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Следующая попытка',
    )
    claim_token = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Токен владельца запуска',
    )
    claimed_until = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Lease запуска истекает',
    )
    total_count = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name='Всего листингов',
    )
    published_count = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name='Опубликовано',
    )
    rejected_count = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name='Отклонено',
    )
    pending_count = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name='Ожидает обработки',
    )
    last_error = models.TextField(
        max_length=2000,
        blank=True,
        editable=False,
        verbose_name='Последняя ошибка',
    )
    finished_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Завершено',
    )
    feed_artifact = models.ForeignKey(
        'MarketplaceFeedArtifact',
        null=True,
        blank=True,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='feed_runs',
        verbose_name='Проверенный артефакт фида',
    )
    artifact_upload_attempt = models.PositiveSmallIntegerField(
        default=0,
        editable=False,
        verbose_name='Попытка загрузки артефакта',
    )
    source_intent_revision = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Исходная ревизия намерения фида',
    )
    endpoint_revision = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Ревизия stable endpoint при запуске',
    )
    predecessor_artifact_id = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='ID предыдущего артефакта фида',
    )

    class Meta:
        verbose_name = 'Запуск фида маркетплейса'
        verbose_name_plural = 'Запуски фидов маркетплейсов'
        constraints = [
            models.UniqueConstraint(
                fields=['account'],
                condition=models.Q(state__in=_MARKETPLACE_FEED_OWNERSHIP_STATES),
                name='uniq_mkt_feed_owner_account',
            ),
            models.UniqueConstraint(
                fields=['account', 'provider_run_id'],
                condition=(
                    models.Q(provider_run_id__isnull=False)
                    & ~models.Q(provider_run_id='')
                ),
                name='uniq_mkt_feed_provider_ref',
            ),
            models.UniqueConstraint(
                fields=['account', 'source_intent_revision'],
                condition=models.Q(source_intent_revision__isnull=False),
                name='uniq_mkt_feed_source_intent',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_intent_revision__isnull=True,
                        endpoint_revision__isnull=True,
                        predecessor_artifact_id__isnull=True,
                        feed_artifact__isnull=True,
                        artifact_upload_attempt=0,
                    )
                    | models.Q(
                        source_intent_revision__gte=1,
                        endpoint_revision__gte=0,
                    )
                ),
                name='mkt_feed_run_mode_bundle',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(feed_artifact__isnull=True)
                    | models.Q(artifact_upload_attempt__gte=1)
                ),
                name='mkt_feed_run_art_attempt',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(feed_artifact__isnull=True)
                    | models.Q(predecessor_artifact_id__isnull=True)
                    | ~models.Q(feed_artifact=models.F('predecessor_artifact_id'))
                ),
                name='mkt_feed_run_art_distinct',
            ),
        ]
        indexes = [
            models.Index(
                fields=['marketplace', 'next_attempt_at', 'id'],
                name='mkt_feed_due_idx',
                condition=models.Q(
                    state__in=_MARKETPLACE_FEED_ACTIVE_STATES,
                    next_attempt_at__isnull=False,
                ),
            ),
            models.Index(
                fields=['feed_artifact', 'id'],
                name='mkt_feed_run_artifact',
                condition=models.Q(feed_artifact__isnull=False),
            ),
        ]

    def __str__(self):
        return f'{self.marketplace}:{self.account_id} [{self.state}] {self.pk}'


class AvitoAccountStatus(TimestampedModel):
    """Последний подтверждённый снимок подключения и тарифа Avito-аккаунта."""

    CONNECTION_UNKNOWN = 'unknown'
    CONNECTION_CONNECTED = 'connected'
    CONNECTION_AUTH_ERROR = 'auth_error'
    CONNECTION_UNAVAILABLE = 'unavailable'
    CONNECTION_CHOICES = [
        (CONNECTION_UNKNOWN, 'Не проверено'),
        (CONNECTION_CONNECTED, 'Подключено'),
        (CONNECTION_AUTH_ERROR, 'Ошибка авторизации'),
        (CONNECTION_UNAVAILABLE, 'Avito временно недоступен'),
    ]

    AUTOLOAD_UNKNOWN = 'unknown'
    AUTOLOAD_ENABLED = 'enabled'
    AUTOLOAD_DISABLED = 'disabled'
    AUTOLOAD_MISSING = 'missing'
    AUTOLOAD_FORBIDDEN = 'forbidden'
    AUTOLOAD_CHOICES = [
        (AUTOLOAD_UNKNOWN, 'Не проверено'),
        (AUTOLOAD_ENABLED, 'Включена'),
        (AUTOLOAD_DISABLED, 'Выключена'),
        (AUTOLOAD_MISSING, 'Профиль отсутствует'),
        (AUTOLOAD_FORBIDDEN, 'Нет доступа'),
    ]

    TARIFF_UNKNOWN = 'unknown'
    TARIFF_ACTIVE = 'active'
    TARIFF_INACTIVE = 'inactive'
    TARIFF_NOT_FOUND = 'not_found'
    TARIFF_UNAVAILABLE = 'unavailable'
    TARIFF_CHOICES = [
        (TARIFF_UNKNOWN, 'Не проверено'),
        (TARIFF_ACTIVE, 'Активен'),
        (TARIFF_INACTIVE, 'Неактивен'),
        (TARIFF_NOT_FOUND, 'Данные недоступны для аккаунта'),
        (TARIFF_UNAVAILABLE, 'Avito временно недоступен'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='avito_account_statuses', verbose_name='Тенант',
    )
    account = models.OneToOneField(
        MarketplaceAccount, on_delete=models.CASCADE,
        related_name='avito_status', verbose_name='Аккаунт Avito',
    )
    connection_status = models.CharField(
        max_length=20, choices=CONNECTION_CHOICES,
        default=CONNECTION_UNKNOWN, verbose_name='Состояние подключения',
    )
    autoload_status = models.CharField(
        max_length=20, choices=AUTOLOAD_CHOICES,
        default=AUTOLOAD_UNKNOWN, verbose_name='Состояние Автозагрузки',
    )
    feed_configured = models.BooleanField(
        null=True, blank=True, verbose_name='Фид MAP настроен',
    )
    profile_checked_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Профиль проверен',
    )
    tariff_status = models.CharField(
        max_length=20, choices=TARIFF_CHOICES,
        default=TARIFF_UNKNOWN, verbose_name='Состояние тарифа',
    )
    tariff_name = models.CharField(max_length=200, blank=True, verbose_name='Тариф')
    tariff_started_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Тариф начался',
    )
    tariff_ends_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Тариф заканчивается',
    )
    tariff_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name='Стоимость тарифа',
    )
    placement_packages = models.JSONField(
        default=list, verbose_name='Пакеты размещений',
    )
    scheduled_tariff = models.JSONField(
        default=dict, verbose_name='Следующий тариф',
    )
    tariff_checked_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Тариф проверен',
    )
    last_attempted_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Последняя попытка проверки',
    )
    last_error_code = models.CharField(max_length=50, blank=True, verbose_name='Код последней ошибки')
    last_error_message = models.CharField(
        max_length=500, blank=True, verbose_name='Последняя ошибка',
    )
    notification_state = models.JSONField(
        default=dict, verbose_name='Отправленные пороги уведомлений',
    )

    class Meta:
        verbose_name = 'Состояние Avito-аккаунта'
        verbose_name_plural = 'Состояния Avito-аккаунтов'
        indexes = [
            models.Index(
                fields=['tenant', 'tariff_status'],
                name='mkt_avito_tenant_tariff_idx',
            ),
            models.Index(
                fields=['tenant', 'autoload_status'],
                name='mkt_avito_tenant_autoload_idx',
            ),
        ]

    def __str__(self):
        return f'{self.account}: {self.autoload_status} / {self.tariff_status}'


class AvitoCategoryTreeSnapshot(TimestampedModel):
    """Последний проверенный снимок дерева категорий из Avito Autoload API."""

    STATUS_READY = 'ready'
    STATUS_ERROR = 'error'
    STATUS_CHOICES = [
        (STATUS_READY, 'Готово'),
        (STATUS_ERROR, 'Ошибка'),
    ]

    domain_slug = models.CharField(
        max_length=50,
        unique=True,
        verbose_name='Домен каталога',
    )
    root_name = models.CharField(max_length=200, verbose_name='Корень Avito')
    tree = models.JSONField(default=list, verbose_name='Дерево')
    checksum = models.CharField(max_length=64, blank=True, verbose_name='Контрольная сумма')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_READY,
        verbose_name='Статус',
    )
    node_count = models.PositiveIntegerField(default=0, verbose_name='Количество узлов')
    change_count = models.PositiveIntegerField(default=0, verbose_name='Изменённых путей')
    fetched_at = models.DateTimeField(null=True, blank=True, verbose_name='Получено из Avito')
    applied_at = models.DateTimeField(null=True, blank=True, verbose_name='Применено к тенантам')
    last_error = models.CharField(max_length=500, blank=True, verbose_name='Последняя ошибка')
    metadata = models.JSONField(default=dict, verbose_name='Метаданные синхронизации')
    source_account = models.ForeignKey(
        MarketplaceAccount,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='category_tree_snapshots',
        verbose_name='Аккаунт-источник',
    )

    class Meta:
        verbose_name = 'Снимок дерева категорий Avito'
        verbose_name_plural = 'Снимки дерева категорий Avito'

    def __str__(self):
        return f'{self.domain_slug}: {self.status} ({self.node_count})'


class MarketplaceFeedEndpoint(TimestampedModel):
    """Stable, provider-neutral public identity for one account feed.

    Each provisioned row is rollout-sticky and freezes the exact legacy
    object/profile locator so an account rename cannot move the feed underneath
    its stable URL. Public serving is controlled by this row's lifecycle, live
    owner generation, and ``serve_enabled`` flag.

    Capability material is deliberately not stored. The current token is
    derived from immutable identity, owner digest, capability revision, and
    ``token_key_id`` by a domain-separated HMAC key ring. During a bounded key
    rotation the verifier also accepts ``previous_token_key_id`` without
    changing the capability revision.
    """

    class StorageMode(models.TextChoices):
        LEGACY_BRIDGE = 'legacy_bridge', 'Мост к legacy-фиду'
        PRIVATE_GENERATION = 'private_generation', 'Приватные поколения фида'

    class ProfileState(models.TextChoices):
        NEW = 'new', 'Создан'
        BRIDGE_READY = 'bridge_ready', 'Legacy-мост готов'
        MIGRATING = 'migrating', 'Профиль переводится'
        UPDATE_UNKNOWN = 'update_unknown', 'Результат обновления неизвестен'
        VERIFIED = 'verified', 'Stable URL подтверждён'
        MANUAL_REVIEW = 'manual_review', 'Требуется ручная сверка'

    public_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name='Публичный ID stable feed endpoint',
    )
    account = models.OneToOneField(
        MarketplaceAccount,
        on_delete=models.CASCADE,
        related_name='feed_endpoint',
        editable=False,
        verbose_name='Аккаунт маркетплейса',
    )
    token_key_id = models.CharField(
        max_length=32,
        editable=False,
        verbose_name='ID HMAC-ключа capability token',
    )
    previous_token_key_id = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        verbose_name='Предыдущий ID HMAC-ключа на время ротации',
    )
    owner_identity_digest = models.CharField(
        max_length=64,
        editable=False,
        verbose_name='Отпечаток provider identity владельца',
    )
    capability_revision = models.PositiveBigIntegerField(
        default=1,
        editable=False,
        verbose_name='Ревизия capability token',
    )
    serve_enabled = models.BooleanField(
        default=False,
        editable=False,
        verbose_name='Публичная выдача разрешена',
    )
    storage_mode = models.CharField(
        max_length=24,
        choices=StorageMode.choices,
        default=StorageMode.LEGACY_BRIDGE,
        editable=False,
        verbose_name='Режим хранения фида',
    )
    legacy_object_key = models.CharField(
        max_length=1024,
        blank=True,
        editable=False,
        verbose_name='Замороженный legacy object key',
    )
    legacy_profile_url = models.URLField(
        max_length=2048,
        blank=True,
        editable=False,
        verbose_name='Точный legacy URL в профиле площадки',
    )
    profile_state = models.CharField(
        max_length=20,
        choices=ProfileState.choices,
        default=ProfileState.NEW,
        editable=False,
        verbose_name='Состояние миграции профиля',
    )
    profile_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name='SHA-256 последнего проверенного профиля',
    )
    profile_revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Ревизия состояния профиля',
    )
    profile_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Профиль площадки проверен',
    )
    current_artifact = models.ForeignKey(
        'MarketplaceFeedArtifact',
        null=True,
        blank=True,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='current_for_endpoints',
        verbose_name='Текущий проверенный артефакт',
    )
    source_intent_revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Текущая желаемая ревизия фида',
    )
    artifact_revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Ревизия текущего артефакта',
    )
    artifact_promoted_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Артефакт назначен текущим',
    )

    class Meta:
        verbose_name = 'Stable feed endpoint маркетплейса'
        verbose_name_plural = 'Stable feed endpoints маркетплейсов'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    token_key_id__regex=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$',
                ),
                name='mkt_feed_ep_key_id_format',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(previous_token_key_id='')
                    | (
                        models.Q(
                            previous_token_key_id__regex=(
                                r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$'
                            ),
                        )
                        & ~models.Q(previous_token_key_id=models.F('token_key_id'))
                    )
                ),
                name='mkt_feed_ep_prev_key',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(previous_token_key_id='')
                    | models.Q(profile_state__in=('migrating', 'update_unknown'))
                ),
                name='mkt_feed_ep_prev_key_state',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    owner_identity_digest__regex=r'^[0-9a-f]{64}$',
                ),
                name='mkt_feed_ep_owner_digest',
            ),
            models.CheckConstraint(
                condition=models.Q(capability_revision__gte=1),
                name='mkt_feed_ep_cap_revision',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    storage_mode__in=(
                        'legacy_bridge',
                        'private_generation',
                    ),
                ),
                name='mkt_feed_ep_storage_mode',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    profile_state__in=(
                        'new',
                        'bridge_ready',
                        'migrating',
                        'update_unknown',
                        'verified',
                        'manual_review',
                    ),
                ),
                name='mkt_feed_ep_profile_state',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(legacy_object_key='', legacy_profile_url='')
                    | (
                        ~models.Q(legacy_object_key='')
                        & models.Q(legacy_profile_url__startswith='https://')
                    )
                ),
                name='mkt_feed_ep_legacy_bundle',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        profile_state__in=(
                            'bridge_ready',
                            'migrating',
                            'update_unknown',
                            'verified',
                        ),
                    )
                    | (
                        models.Q(storage_mode='legacy_bridge')
                        & ~models.Q(legacy_object_key='')
                    )
                    | (
                        models.Q(storage_mode='private_generation')
                        & (
                            (
                                models.Q(serve_enabled=False)
                                & ~models.Q(legacy_object_key='')
                            )
                            | models.Q(current_artifact__isnull=False)
                        )
                    )
                ),
                name='mkt_feed_ep_state_legacy',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        profile_fingerprint='',
                        profile_verified_at__isnull=True,
                    )
                    | models.Q(
                        profile_fingerprint__regex=r'^[0-9a-f]{64}$',
                        profile_verified_at__isnull=False,
                    )
                ),
                name='mkt_feed_ep_profile_baseline',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        profile_state__in=(
                            'bridge_ready',
                            'migrating',
                            'update_unknown',
                            'verified',
                        ),
                    )
                    | models.Q(
                        profile_fingerprint__regex=r'^[0-9a-f]{64}$',
                        profile_verified_at__isnull=False,
                    )
                ),
                name='mkt_feed_ep_servable_baseline',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(serve_enabled=False)
                    | (
                        models.Q(
                            profile_state__in=(
                                'bridge_ready',
                                'migrating',
                                'update_unknown',
                                'verified',
                            ),
                        )
                        & (
                            (
                                models.Q(storage_mode='legacy_bridge')
                                & ~models.Q(legacy_object_key='')
                            )
                            | (
                                models.Q(storage_mode='private_generation')
                                & models.Q(current_artifact__isnull=False)
                            )
                        )
                    )
                ),
                name='mkt_feed_ep_serve_guard',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        current_artifact__isnull=True,
                        artifact_revision=0,
                        artifact_promoted_at__isnull=True,
                    )
                    | models.Q(
                        current_artifact__isnull=False,
                        artifact_revision__gte=1,
                        artifact_promoted_at__isnull=False,
                    )
                ),
                name='mkt_feed_ep_art_bundle',
            ),
        ]
        indexes = [
            models.Index(
                fields=['profile_state', 'updated_at', 'public_id'],
                name='mkt_feed_ep_state_updated',
            ),
            models.Index(
                fields=['current_artifact', 'public_id'],
                name='mkt_feed_ep_current_art',
                condition=models.Q(current_artifact__isnull=False),
            ),
        ]

    def __str__(self):
        return f'{self.account_id} [{self.profile_state}] {self.public_id}'


class MarketplaceFeedArtifactUploadAttempt(TimestampedModel):
    """Durable, redaction-safe journal for one immutable object PUT attempt.

    The row is prepared before crossing the object-storage boundary.  Its
    projection metadata is the authoritative snapshot used to verify and
    attach an artifact; mutable feed-run counters are deliberately not part of
    that contract.
    """

    class State(models.TextChoices):
        PREPARED = 'prepared', 'Подготовлена'
        PUT_PENDING = 'put_pending', 'PUT выполняется или требует сверки'
        VERSION_KNOWN = 'version_known', 'VersionId подтверждён'
        VERIFIED = 'verified', 'Версия проверена чтением'
        ATTACHED = 'attached', 'Артефакт атомарно привязан'
        NO_OBJECT = 'no_object', 'Отсутствие объекта подтверждено'
        ORPHANED = 'orphaned', 'Объект оставлен для безопасной очистки'
        MANUAL_REVIEW = 'manual_review', 'Требуется ручная сверка'

    class ResolutionSource(models.TextChoices):
        PUT_RESPONSE = 'put_response', 'Ответ одиночного PUT'
        OPERATOR_RECONCILIATION = (
            'operator_reconciliation',
            'Операторская сверка',
        )

    ACTIVE_STATES = (
        State.PREPARED,
        State.PUT_PENDING,
        State.VERSION_KNOWN,
        State.VERIFIED,
    )
    TERMINAL_STATES = (
        State.ATTACHED,
        State.NO_OBJECT,
        State.ORPHANED,
        State.MANUAL_REVIEW,
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        MarketplaceAccount,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='feed_artifact_upload_attempts',
        verbose_name='Аккаунт маркетплейса',
    )
    endpoint = models.ForeignKey(
        MarketplaceFeedEndpoint,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='artifact_upload_attempts',
        verbose_name='Stable feed endpoint',
    )
    run = models.ForeignKey(
        MarketplaceFeedRun,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='artifact_upload_attempts',
        verbose_name='Запуск фида',
    )
    attempt_no = models.PositiveSmallIntegerField(
        editable=False,
        verbose_name='Номер попытки загрузки',
    )
    revision = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        verbose_name='Ревизия журнала загрузки',
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PREPARED,
        editable=False,
        verbose_name='Состояние попытки загрузки',
    )
    put_resolution_source = models.CharField(
        max_length=32,
        choices=ResolutionSource.choices,
        default='',
        blank=True,
        editable=False,
        verbose_name='Источник разрешения PUT',
    )
    storage_bucket = models.CharField(
        max_length=63,
        editable=False,
        verbose_name='Приватный bucket',
    )
    expected_bucket_owner = models.CharField(
        max_length=255,
        editable=False,
        verbose_name='Ожидаемый владелец приватного bucket',
    )
    object_key = models.CharField(
        max_length=255,
        editable=False,
        verbose_name='Ключ объекта',
    )
    payload_sha256 = models.CharField(
        max_length=64,
        editable=False,
        verbose_name='SHA-256 содержимого',
    )
    size_bytes = models.PositiveBigIntegerField(
        editable=False,
        verbose_name='Размер объекта в байтах',
    )
    projection_count = models.PositiveIntegerField(
        editable=False,
        verbose_name='Количество листингов в проекции',
    )
    content_type = models.CharField(
        max_length=64,
        editable=False,
        verbose_name='Content-Type объекта',
    )
    put_run_revision = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Ревизия запуска перед PUT',
    )
    object_version_id = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        editable=False,
        verbose_name='Точный VersionId объекта',
    )
    put_started_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='PUT начат',
    )
    version_known_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='VersionId подтверждён',
    )
    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Версия проверена',
    )
    attached_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Артефакт привязан',
    )
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Попытка окончательно разрешена',
    )
    safe_error_code = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name='Безопасный код ошибки',
    )

    class Meta:
        verbose_name = 'Попытка загрузки артефакта фида'
        verbose_name_plural = 'Попытки загрузки артефактов фида'
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'attempt_no'],
                name='uniq_mkt_upl_run_attempt',
            ),
            models.UniqueConstraint(
                fields=['storage_bucket', 'object_key'],
                name='uniq_mkt_upl_object',
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=(
                    'prepared',
                    'put_pending',
                    'version_known',
                    'verified',
                    'attached',
                    'no_object',
                    'orphaned',
                    'manual_review',
                )),
                name='mkt_upl_state',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    put_resolution_source__in=(
                        '',
                        'put_response',
                        'operator_reconciliation',
                    ),
                ),
                name='mkt_upl_resolution_src',
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_no__gte=1, attempt_no__lte=32767),
                name='mkt_upl_attempt',
            ),
            models.CheckConstraint(
                condition=models.Q(payload_sha256__regex=r'^[0-9a-f]{64}$'),
                name='mkt_upl_payload_sha',
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gte=1, size_bytes__lte=1073741824),
                name='mkt_upl_size',
            ),
            models.CheckConstraint(
                condition=models.Q(projection_count__lte=10000),
                name='mkt_upl_projection',
            ),
            models.CheckConstraint(
                condition=models.Q(content_type='application/xml'),
                name='mkt_upl_content_type',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    storage_bucket__regex=(
                        r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
                    ),
                ),
                name='mkt_upl_bucket',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    expected_bucket_owner__regex=(
                        r'^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,254}$'
                    ),
                ),
                name='mkt_upl_bucket_owner',
            ),
            models.CheckConstraint(
                condition=models.Q(object_key__startswith='private-feeds/v1/'),
                name='mkt_upl_object_key',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(safe_error_code='')
                    | models.Q(
                        safe_error_code__regex=r'^[a-z][a-z0-9_]{0,63}$',
                    )
                ),
                name='mkt_upl_error_code',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        put_run_revision__isnull=True,
                        put_started_at__isnull=True,
                    )
                    | models.Q(
                        put_run_revision__isnull=False,
                        put_started_at__isnull=False,
                    )
                ),
                name='mkt_upl_put_bundle',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        object_version_id__isnull=True,
                        version_known_at__isnull=True,
                    )
                    | (
                        models.Q(
                            object_version_id__isnull=False,
                            version_known_at__isnull=False,
                        )
                        & ~models.Q(object_version_id='')
                        & ~models.Q(object_version_id__iregex=r'^null$')
                    )
                ),
                name='mkt_upl_version_bundle',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(verified_at__isnull=True)
                    | models.Q(object_version_id__isnull=False)
                ),
                name='mkt_upl_verified_version',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state='prepared',
                        put_run_revision__isnull=True,
                        object_version_id__isnull=True,
                        verified_at__isnull=True,
                        attached_at__isnull=True,
                        resolved_at__isnull=True,
                        safe_error_code='',
                    )
                    | models.Q(
                        state='put_pending',
                        put_run_revision__isnull=False,
                        object_version_id__isnull=True,
                        verified_at__isnull=True,
                        attached_at__isnull=True,
                        resolved_at__isnull=True,
                        safe_error_code='',
                    )
                    | models.Q(
                        state='version_known',
                        put_run_revision__isnull=False,
                        object_version_id__isnull=False,
                        verified_at__isnull=True,
                        attached_at__isnull=True,
                        resolved_at__isnull=True,
                        safe_error_code='',
                    )
                    | models.Q(
                        state='verified',
                        put_run_revision__isnull=False,
                        object_version_id__isnull=False,
                        verified_at__isnull=False,
                        attached_at__isnull=True,
                        resolved_at__isnull=True,
                        safe_error_code='',
                    )
                    | models.Q(
                        state='attached',
                        put_run_revision__isnull=False,
                        object_version_id__isnull=False,
                        verified_at__isnull=False,
                        attached_at__isnull=False,
                        resolved_at__isnull=False,
                        safe_error_code='',
                    )
                    | (
                        models.Q(
                            state='no_object',
                            object_version_id__isnull=True,
                            verified_at__isnull=True,
                            attached_at__isnull=True,
                            resolved_at__isnull=False,
                        )
                        & ~models.Q(safe_error_code='')
                    )
                    | (
                        models.Q(
                            state='orphaned',
                            put_run_revision__isnull=False,
                            object_version_id__isnull=False,
                            attached_at__isnull=True,
                            resolved_at__isnull=False,
                        )
                        & ~models.Q(safe_error_code='')
                    )
                    | (
                        models.Q(
                            state='manual_review',
                            attached_at__isnull=True,
                            resolved_at__isnull=False,
                        )
                        & ~models.Q(safe_error_code='')
                    )
                ),
                name='mkt_upl_state_bundle',
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(put_started_at__isnull=True)
                        | models.Q(put_started_at__gte=models.F('created_at'))
                    )
                    & (
                        models.Q(version_known_at__isnull=True)
                        | models.Q(version_known_at__gte=models.F('put_started_at'))
                    )
                    & (
                        models.Q(verified_at__isnull=True)
                        | models.Q(verified_at__gte=models.F('version_known_at'))
                    )
                    & (
                        models.Q(attached_at__isnull=True)
                        | models.Q(attached_at__gte=models.F('verified_at'))
                    )
                    & (
                        models.Q(resolved_at__isnull=True)
                        | models.Q(resolved_at__gte=models.F('created_at'))
                    )
                    & (
                        models.Q(resolved_at__isnull=True)
                        | models.Q(put_started_at__isnull=True)
                        | models.Q(resolved_at__gte=models.F('put_started_at'))
                    )
                    & (
                        models.Q(resolved_at__isnull=True)
                        | models.Q(version_known_at__isnull=True)
                        | models.Q(resolved_at__gte=models.F('version_known_at'))
                    )
                    & (
                        models.Q(resolved_at__isnull=True)
                        | models.Q(verified_at__isnull=True)
                        | models.Q(resolved_at__gte=models.F('verified_at'))
                    )
                ),
                name='mkt_upl_time_order',
            ),
        ]
        indexes = [
            models.Index(
                fields=['state', 'updated_at', 'id'],
                name='mkt_upl_state_updated',
            ),
            models.Index(
                fields=['account', '-created_at', 'id'],
                name='mkt_upl_acct_created',
            ),
            models.Index(
                fields=['endpoint', '-created_at', 'id'],
                name='mkt_upl_ep_created',
            ),
        ]

    def __str__(self):
        return f'{self.account_id} [{self.state}] {self.pk}'


class MarketplaceFeedPutReconciliationAudit(models.Model):
    """Immutable, locator-free evidence for an operator PUT decision."""

    class Outcome(models.TextChoices):
        NO_OBJECT_BY_REVIEWED_SETTLEMENT_POLICY = (
            'no_object_by_reviewed_settlement_policy',
            'Объект не найден после выдержки',
        )
        VERSION_KNOWN = 'version_known', 'VersionId подтверждён'
        MANUAL_REVIEW = 'manual_review', 'Требуется ручная сверка'

    FROM_STATE = MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    TO_STATES = (
        MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
        MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
    )
    MANUAL_DECISION_CODES = (
        'put_reconcile_delete_marker',
        'put_reconcile_multiple_versions',
        'put_reconcile_unusable_version',
        'put_reconcile_malformed_listing',
        'put_reconcile_page_limit',
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.OneToOneField(
        MarketplaceFeedArtifactUploadAttempt,
        editable=False,
        on_delete=models.PROTECT,
        related_name='put_reconciliation_audit',
        verbose_name='Попытка загрузки',
    )
    pre_revision = models.PositiveBigIntegerField(editable=False)
    post_revision = models.PositiveBigIntegerField(editable=False)
    from_state = models.CharField(max_length=20, editable=False)
    to_state = models.CharField(max_length=20, editable=False)
    outcome = models.CharField(
        max_length=48,
        choices=Outcome.choices,
        editable=False,
    )
    decision_code = models.CharField(max_length=64, blank=True, editable=False)
    version_id_captured = models.BooleanField(editable=False)
    origin_process_identity_digest = models.CharField(max_length=64, editable=False)
    operator_identity_digest = models.CharField(max_length=64, editable=False)
    evidence_digest = models.CharField(max_length=64, editable=False)
    digest_scheme_revision = models.CharField(max_length=64, editable=False)
    identity_digest_key_revision = models.CharField(max_length=64, editable=False)
    adapter_policy_revision = models.CharField(max_length=64, editable=False)
    canary_policy_revision = models.CharField(max_length=64, editable=False)
    origin_process_terminated_at = models.DateTimeField(editable=False)
    reconciliation_started_at = models.DateTimeField(editable=False)
    decision_at = models.DateTimeField(editable=False)
    settlement_window_seconds = models.PositiveIntegerField(editable=False)
    pages_scanned = models.PositiveSmallIntegerField(editable=False)
    entries_scanned = models.PositiveSmallIntegerField(editable=False)
    exact_version_count = models.PositiveSmallIntegerField(editable=False)
    exact_delete_marker_count = models.PositiveSmallIntegerField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        verbose_name = 'Аудит операторской PUT-сверки'
        verbose_name_plural = 'Аудит PUT-сверок'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(pre_revision__gte=1),
                name='mkt_put_aud_pre_revision',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    post_revision=models.F('pre_revision') + 1,
                ),
                name='mkt_put_aud_revision_step',
            ),
            models.CheckConstraint(
                condition=models.Q(from_state='put_pending'),
                name='mkt_put_aud_from_state',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    to_state__in=('no_object', 'version_known', 'manual_review'),
                ),
                name='mkt_put_aud_to_state',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        to_state='no_object',
                        outcome='no_object_by_reviewed_settlement_policy',
                        decision_code='reviewed_settlement_no_object',
                        version_id_captured=False,
                        exact_version_count=0,
                        exact_delete_marker_count=0,
                    )
                    | models.Q(
                        to_state='version_known',
                        outcome='version_known',
                        decision_code='',
                        version_id_captured=True,
                        exact_version_count=1,
                        exact_delete_marker_count=0,
                    )
                    | (
                        models.Q(
                            to_state='manual_review',
                            outcome='manual_review',
                            decision_code__in=(
                                'put_reconcile_delete_marker',
                                'put_reconcile_multiple_versions',
                                'put_reconcile_unusable_version',
                                'put_reconcile_malformed_listing',
                                'put_reconcile_page_limit',
                            ),
                        )
                        & (
                            models.Q(version_id_captured=False)
                            | models.Q(
                                version_id_captured=True,
                                exact_version_count__gte=1,
                            )
                        )
                    )
                ),
                name='mkt_put_aud_decision_bundle',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        origin_process_identity_digest__regex=r'^[0-9a-f]{64}$',
                    )
                    & models.Q(
                        operator_identity_digest__regex=r'^[0-9a-f]{64}$',
                    )
                    & models.Q(evidence_digest__regex=r'^[0-9a-f]{64}$')
                ),
                name='mkt_put_aud_digests',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        digest_scheme_revision__regex=(
                            r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'
                        ),
                    )
                    & models.Q(
                        identity_digest_key_revision__regex=(
                            r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'
                        ),
                    )
                    & models.Q(
                        adapter_policy_revision__regex=(
                            r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'
                        ),
                    )
                    & models.Q(
                        canary_policy_revision__regex=(
                            r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'
                        ),
                    )
                ),
                name='mkt_put_aud_policy_tokens',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    settlement_window_seconds=900,
                    pages_scanned__gte=1,
                    pages_scanned__lte=4,
                    entries_scanned__lte=400,
                    exact_version_count__lte=400,
                    exact_delete_marker_count__lte=400,
                ),
                name='mkt_put_aud_bounded_counts',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(exact_version_count__lte=models.F('entries_scanned'))
                    & models.Q(
                        exact_delete_marker_count__lte=models.F('entries_scanned'),
                    )
                    & models.Q(
                        entries_scanned__gte=(
                            models.F('exact_version_count')
                            + models.F('exact_delete_marker_count')
                        ),
                    )
                ),
                name='mkt_put_aud_count_order',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        reconciliation_started_at__gte=(
                            models.F('origin_process_terminated_at')
                            + models.ExpressionWrapper(
                                models.F('settlement_window_seconds')
                                * models.Value(timedelta(seconds=1)),
                                output_field=models.DurationField(),
                            )
                        ),
                    )
                    & models.Q(
                        decision_at__gte=models.F('reconciliation_started_at'),
                    )
                ),
                name='mkt_put_aud_time_order',
            ),
        ]

    def __str__(self):
        return f'{self.attempt_id} [{self.outcome}] {self.pk}'


class MarketplaceFeedArtifact(models.Model):
    """Immutable metadata for one content-addressed, verified feed object."""

    CONTENT_TYPE_XML = 'application/xml'
    VERIFICATION_VERSION_READBACK_SHA256 = 'version_readback_sha256'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(
        auto_now_add=True,
        editable=False,
        verbose_name='Создано',
    )
    endpoint = models.ForeignKey(
        MarketplaceFeedEndpoint,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='artifacts',
        verbose_name='Stable feed endpoint',
    )
    account = models.ForeignKey(
        MarketplaceAccount,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='feed_artifacts',
        verbose_name='Аккаунт маркетплейса',
    )
    run = models.ForeignKey(
        MarketplaceFeedRun,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='artifacts',
        verbose_name='Запуск фида',
    )
    upload_attempt = models.PositiveSmallIntegerField(
        editable=False,
        verbose_name='Попытка загрузки',
    )
    storage_bucket = models.CharField(
        max_length=63,
        editable=False,
        verbose_name='Приватный bucket',
    )
    object_key = models.CharField(
        max_length=255,
        editable=False,
        verbose_name='Ключ объекта',
    )
    object_version_id = models.CharField(
        max_length=1024,
        editable=False,
        verbose_name='VersionId объекта',
    )
    payload_sha256 = models.CharField(
        max_length=64,
        editable=False,
        verbose_name='SHA-256 содержимого',
    )
    size_bytes = models.PositiveBigIntegerField(
        editable=False,
        verbose_name='Размер объекта в байтах',
    )
    listing_count = models.PositiveIntegerField(
        editable=False,
        verbose_name='Количество листингов',
    )
    content_type = models.CharField(
        max_length=64,
        editable=False,
        verbose_name='Content-Type объекта',
    )
    verification_method = models.CharField(
        max_length=32,
        editable=False,
        verbose_name='Метод проверки объекта',
    )
    verified_at = models.DateTimeField(
        editable=False,
        verbose_name='Объект проверен',
    )

    class Meta:
        verbose_name = 'Проверенный артефакт фида'
        verbose_name_plural = 'Проверенные артефакты фидов'
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'upload_attempt'],
                name='uniq_mkt_art_run_attempt',
            ),
            models.UniqueConstraint(
                fields=['storage_bucket', 'object_key'],
                name='uniq_mkt_art_object',
            ),
            models.CheckConstraint(
                condition=models.Q(content_type='application/xml'),
                name='mkt_art_content_type',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    verification_method='version_readback_sha256',
                ),
                name='mkt_art_verify_method',
            ),
            models.CheckConstraint(
                condition=models.Q(payload_sha256__regex=r'^[0-9a-f]{64}$'),
                name='mkt_art_payload_sha',
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gte=1, size_bytes__lte=1073741824),
                name='mkt_art_size',
            ),
            models.CheckConstraint(
                condition=models.Q(listing_count__lte=10000),
                name='mkt_art_listing_count',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    upload_attempt__gte=1,
                    upload_attempt__lte=32767,
                ),
                name='mkt_art_upload_attempt',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(object_version_id='')
                    & ~models.Q(object_version_id__iregex=r'^null$')
                ),
                name='mkt_art_version_present',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    storage_bucket__regex=(
                        r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
                    ),
                ),
                name='mkt_art_bucket_format',
            ),
            models.CheckConstraint(
                condition=models.Q(object_key__startswith='private-feeds/v1/'),
                name='mkt_art_object_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['endpoint', '-verified_at', 'id'],
                name='mkt_art_ep_verified',
            ),
            models.Index(
                fields=['account', '-verified_at', 'id'],
                name='mkt_art_acct_verified',
            ),
            models.Index(
                fields=['verified_at', 'id'],
                name='mkt_art_verified',
            ),
        ]

    def __str__(self):
        return f'{self.account_id} [{self.verified_at:%Y-%m-%d %H:%M:%S}] {self.pk}'


class MarketplaceFeedFetchEvidence(models.Model):
    """Append-only evidence for an authorized stable-feed redirect."""

    class RequestMethod(models.TextChoices):
        GET = 'GET', 'GET'
        HEAD = 'HEAD', 'HEAD'

    id = models.BigAutoField(primary_key=True)
    issued_at = models.DateTimeField(
        auto_now_add=True,
        editable=False,
        verbose_name='Redirect выдан',
    )
    endpoint = models.ForeignKey(
        MarketplaceFeedEndpoint,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='fetch_evidence',
        verbose_name='Stable feed endpoint',
    )
    artifact = models.ForeignKey(
        MarketplaceFeedArtifact,
        db_index=False,
        editable=False,
        on_delete=models.PROTECT,
        related_name='fetch_evidence',
        verbose_name='Выданный артефакт',
    )
    request_method = models.CharField(
        max_length=4,
        choices=RequestMethod.choices,
        editable=False,
        verbose_name='HTTP-метод',
    )
    accepted_token_key_id = models.CharField(
        max_length=32,
        editable=False,
        verbose_name='Принятый ID capability-ключа',
    )
    capability_revision = models.PositiveBigIntegerField(
        editable=False,
        verbose_name='Ревизия capability token',
    )
    endpoint_revision = models.PositiveBigIntegerField(
        editable=False,
        verbose_name='Ревизия stable endpoint',
    )
    source_intent_revision = models.PositiveBigIntegerField(
        editable=False,
        verbose_name='Ревизия намерения фида',
    )
    run_revision = models.PositiveBigIntegerField(
        editable=False,
        verbose_name='Ревизия запуска фида',
    )
    redirect_status = models.PositiveSmallIntegerField(
        default=307,
        editable=False,
        verbose_name='HTTP-статус redirect',
    )
    redirect_expires_at = models.DateTimeField(
        editable=False,
        verbose_name='Redirect истекает',
    )

    class Meta:
        verbose_name = 'Свидетельство выдачи артефакта фида'
        verbose_name_plural = 'Свидетельства выдачи артефактов фида'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(request_method__in=('GET', 'HEAD')),
                name='mkt_fetch_method',
            ),
            models.CheckConstraint(
                condition=models.Q(redirect_status=307),
                name='mkt_fetch_status',
            ),
            models.CheckConstraint(
                condition=models.Q(redirect_expires_at__gt=models.F('issued_at')),
                name='mkt_fetch_expiry',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    accepted_token_key_id__regex=(
                        r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$'
                    ),
                ),
                name='mkt_fetch_key_id',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    capability_revision__gte=1,
                    endpoint_revision__gte=1,
                    source_intent_revision__gte=1,
                    run_revision__gte=0,
                ),
                name='mkt_fetch_revisions',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    redirect_expires_at__lte=(
                        models.F('issued_at') + timedelta(seconds=300)
                    ),
                ),
                name='mkt_fetch_ttl',
            ),
        ]
        indexes = [
            models.Index(
                fields=['endpoint', '-issued_at', 'id'],
                name='mkt_fetch_ep_issued',
            ),
            models.Index(
                fields=['artifact', '-issued_at', 'id'],
                name='mkt_fetch_art_issued',
            ),
            models.Index(
                fields=['-issued_at', 'id'],
                name='mkt_fetch_issued',
            ),
        ]

    def __str__(self):
        return f'{self.endpoint_id} [{self.request_method} {self.redirect_status}] {self.pk}'


class MarketplacePlacementAddress(TimestampedModel):
    """Сохранённый адрес размещения для аккаунта маркетплейса."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='marketplace_placement_addresses', verbose_name='Тенант',
    )
    account = models.ForeignKey(
        MarketplaceAccount, on_delete=models.CASCADE,
        related_name='placement_addresses', verbose_name='Аккаунт Avito',
    )
    name = models.CharField(max_length=200, verbose_name='Название')
    seller_address_id = models.CharField(max_length=100, blank=True, verbose_name='ID адреса Avito')
    address = models.CharField(max_length=500, blank=True, verbose_name='Регион/адрес размещения')
    manager_name = models.CharField(max_length=100, blank=True, verbose_name='Контактное лицо')
    contact_phone = models.CharField(max_length=50, blank=True, verbose_name='Контактный телефон')
    is_default = models.BooleanField(default=False, verbose_name='По умолчанию')
    is_active = models.BooleanField(default=True, verbose_name='Активен')

    class Meta:
        verbose_name = 'Адрес размещения'
        verbose_name_plural = 'Адреса размещения'
        ordering = ['account', '-is_default', 'name']
        unique_together = [('tenant', 'account', 'name')]

    def __str__(self):
        return f'{self.account.name}: {self.name}'


class Listing(SoftDeleteModel):
    STATUS_DRAFT = 'draft'
    STATUS_QUEUED = 'queued'
    STATUS_PENDING = 'pending'
    STATUS_ACTIVE = 'active'
    STATUS_REJECTED = 'rejected'
    STATUS_ARCHIVING = 'archiving'
    STATUS_ARCHIVED = 'archived'
    STATUS_DELETED = 'deleted'
    STATUS_REQUIRES_REVIEW = 'requires_review'
    STATUS_LIMIT_REACHED = 'limit_reached'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Черновик'),
        (STATUS_QUEUED, 'В очереди'),
        (STATUS_PENDING, 'На модерации Avito'),
        (STATUS_ACTIVE, 'Активно'),
        (STATUS_REJECTED, 'Отклонено'),
        (STATUS_ARCHIVING, 'Снимается (ждёт Avito)'),
        (STATUS_ARCHIVED, 'В архиве'),
        (STATUS_DELETED, 'Удалено'),
        (STATUS_REQUIRES_REVIEW, 'Требует проверки'),
        (STATUS_LIMIT_REACHED, 'Лимит достигнут'),
    ]

    REMOTE_STATUS_ACTIVE = 'active'
    REMOTE_STATUS_REJECTED = 'rejected'
    REMOTE_STATUS_BLOCKED = 'blocked'
    REMOTE_STATUS_REMOVED = 'removed'
    REMOTE_STATUS_ARCHIVED = 'archived'
    REMOTE_STATUS_OTHER = 'other'
    REMOTE_STATUS_CHOICES = [
        (REMOTE_STATUS_ACTIVE, 'Активно на площадке'),
        (REMOTE_STATUS_REJECTED, 'Отклонено площадкой'),
        (REMOTE_STATUS_BLOCKED, 'Заблокировано площадкой'),
        (REMOTE_STATUS_REMOVED, 'Удалено с площадки'),
        (REMOTE_STATUS_ARCHIVED, 'В архиве площадки'),
        (REMOTE_STATUS_OTHER, 'Другой нормализованный статус'),
    ]

    # Вид объявления Avito (тег <AdType> в фиде Autoload). Значения — точные
    # строки, которые принимает Avito; в БД храним их же. «Продаю своё» Avito
    # не принимает для категории «Запчасти и аксессуары», поэтому его нет.
    AD_TYPE_RESALE = 'Товар приобретен на продажу'
    AD_TYPE_MANUFACTURER = 'Товар от производителя'
    AD_TYPE_CHOICES = [
        (AD_TYPE_RESALE, 'Товар приобретён на продажу — перепродажа (B2B)'),
        (AD_TYPE_MANUFACTURER, 'Товар от производителя'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='listings', verbose_name='Тенант')
    product = models.ForeignKey(
        'products.Product', on_delete=models.CASCADE,
        related_name='listings', verbose_name='Товар',
    )
    account = models.ForeignKey(
        MarketplaceAccount, on_delete=models.CASCADE,
        related_name='listings', verbose_name='Аккаунт Avito',
    )
    feed_run = models.ForeignKey(
        MarketplaceFeedRun,
        null=True,
        blank=True,
        db_index=False,
        editable=False,
        on_delete=models.SET_NULL,
        related_name='listings',
        verbose_name='Поколение фида',
    )
    external_id = models.CharField(
        max_length=100, null=True, blank=True, unique=True, verbose_name='ID объявления Avito',
    )
    external_url = models.URLField(blank=True, verbose_name='Ссылка на Avito')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name='Статус')
    rejection_reason = models.TextField(blank=True, verbose_name='Причина отклонения')
    title = models.CharField(max_length=300, blank=True, verbose_name='Заголовок объявления')
    description_ai = models.TextField(blank=True, verbose_name='AI-описание')
    ai_confidence = models.FloatField(null=True, blank=True, verbose_name='Уверенность AI (0–1)')
    price_on_listing = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='Цена на объявлении, ₽')
    margin_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        verbose_name='Наценка листинга, % (override категории)',
    )
    publish_idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, verbose_name='Ключ идемпотентности')
    ad_type = models.CharField(
        max_length=50, choices=AD_TYPE_CHOICES, default=AD_TYPE_RESALE,
        verbose_name='Вид объявления Avito',
    )
    placement_address = models.ForeignKey(
        MarketplacePlacementAddress, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='manual_listings', verbose_name='Адрес размещения',
    )
    address_override = models.CharField(max_length=500, blank=True, verbose_name='Адрес объявления')
    seller_address_id_override = models.CharField(
        max_length=100, blank=True, verbose_name='ID адреса продавца Avito',
    )
    manager_name_override = models.CharField(max_length=100, blank=True, verbose_name='Контактное лицо')
    contact_phone_override = models.CharField(max_length=50, blank=True, verbose_name='Контактный телефон')
    bulk_address = models.CharField(max_length=500, blank=True, verbose_name='Массово назначенный адрес')
    bulk_seller_address_id = models.CharField(
        max_length=100, blank=True, verbose_name='Массово назначенный ID адреса продавца Avito',
    )
    bulk_manager_name = models.CharField(max_length=100, blank=True, verbose_name='Массово назначенное контактное лицо')
    bulk_contact_phone = models.CharField(max_length=50, blank=True, verbose_name='Массово назначенный телефон')
    bulk_placement_address = models.ForeignKey(
        MarketplacePlacementAddress, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='bulk_listings', verbose_name='Массово назначенный адрес размещения',
    )
    retry_count = models.PositiveSmallIntegerField(default=0, verbose_name='Количество попыток')
    next_retry_at = models.DateTimeField(null=True, blank=True, verbose_name='Следующая попытка')
    published_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата публикации')
    last_sync_at = models.DateTimeField(null=True, blank=True, verbose_name='Последняя синхронизация')
    # The canonical lifecycle remains ``status``. These fields store a bounded
    # provider observation and a future due cursor without inferring remote
    # truth from local state.
    remote_status = models.CharField(
        max_length=32,
        choices=REMOTE_STATUS_CHOICES,
        null=True,
        blank=True,
        editable=False,
        verbose_name='Последний статус на площадке',
    )
    remote_status_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Статус на площадке проверен',
    )
    next_status_check_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Следующая проверка статуса',
    )
    status_check_claim_token = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Токен batch-проверки статуса',
    )
    status_check_claimed_until = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Lease проверки статуса истекает',
    )

    class Meta:
        verbose_name = 'Листинг'
        verbose_name_plural = 'Листинги'
        unique_together = [('tenant', 'product', 'account')]
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['account', 'status']),
            models.Index(fields=['next_retry_at']),
            models.Index(
                fields=['account', 'status', 'next_status_check_at', 'id'],
                name='mkt_lst_acct_stat_due',
                condition=models.Q(
                    deleted_at__isnull=True,
                    external_id__isnull=False,
                    next_status_check_at__isnull=False,
                ) & ~models.Q(external_id=''),
            ),
            models.Index(
                fields=['feed_run', 'status', 'id'],
                name='mkt_lst_feed_pending',
                condition=models.Q(
                    deleted_at__isnull=True,
                    external_id__isnull=True,
                    feed_run__isnull=False,
                ),
            ),
        ]

    def __str__(self):
        return f'Листинг #{self.pk} [{self.status}]'


class ListingStats(models.Model):
    """Ежедневный снимок статистики листинга с Avito."""

    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name='stats', verbose_name='Листинг',
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='listing_stats', verbose_name='Тенант',
    )
    date = models.DateField(verbose_name='Дата')
    views = models.PositiveIntegerField(default=0, verbose_name='Просмотры')
    contacts = models.PositiveIntegerField(default=0, verbose_name='Контакты')
    impressions = models.PositiveIntegerField(default=0, verbose_name='Показы')
    ctr = models.FloatField(default=0.0, verbose_name='CTR')

    class Meta:
        unique_together = [('listing', 'date')]
        indexes = [
            models.Index(fields=['tenant', 'date']),
        ]

    def __str__(self):
        return f'Stats listing#{self.listing_id} {self.date}'
