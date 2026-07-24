import uuid

from django.db import models

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


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


class MarketplaceAccount(TimestampedModel):
    """Аккаунт маркетплейса (Avito) привязанный к тенанту."""

    MARKETPLACE_AVITO = 'avito'
    MARKETPLACE_CHOICES = [(MARKETPLACE_AVITO, 'Avito')]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='marketplace_accounts', verbose_name='Тенант',
    )
    marketplace = models.CharField(
        max_length=50, choices=MARKETPLACE_CHOICES,
        default=MARKETPLACE_AVITO, verbose_name='Маркетплейс',
    )
    name = models.CharField(max_length=200, verbose_name='Название аккаунта')
    external_id = models.CharField(max_length=100, verbose_name='ID пользователя на Avito')
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
    # Когда последний раз триггерили Autoload. Avito читает фид ~раз в час, поэтому
    # изменения копятся в окне и уходят одним фидом (см. request_feed_flush).
    last_feed_flush_at = models.DateTimeField(null=True, blank=True, verbose_name='Последняя автозагрузка фида')

    class Meta:
        verbose_name = 'Avito-аккаунт'
        verbose_name_plural = 'Avito-аккаунты'
        unique_together = [('tenant', 'marketplace', 'external_id')]

    def __str__(self):
        return f'{self.tenant.slug} / {self.name}'


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


class Listing(TimestampedModel):
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

    class Meta:
        verbose_name = 'Листинг'
        verbose_name_plural = 'Листинги'
        unique_together = [('tenant', 'product', 'account')]
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['account', 'status']),
            models.Index(fields=['next_retry_at']),
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
