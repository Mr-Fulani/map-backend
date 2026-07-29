import hashlib
import secrets

from django.conf import settings
from django.db import models

from apps.core.models import TimestampedModel

WEBHOOK_EVENTS = [
    'listing.published',
    'listing.rejected',
    'listing.archived',
    'import.completed',
    'import.failed',
    'billing.payment_success',
    'billing.payment_failed',
]


class CatalogDomain(TimestampedModel):
    """Platform-level домен каталога, которым управляет суперюзер."""

    slug = models.SlugField(max_length=50, unique=True, verbose_name='Slug')
    name = models.CharField(max_length=120, verbose_name='Название')
    short_name = models.CharField(max_length=60, blank=True, verbose_name='Короткое название')
    description = models.TextField(blank=True, verbose_name='Описание')
    seo_title = models.CharField(max_length=255, blank=True, verbose_name='SEO title')
    seo_description = models.TextField(blank=True, verbose_name='SEO description')
    seo_keywords = models.CharField(max_length=500, blank=True, verbose_name='SEO keywords')
    seo_h1 = models.CharField(max_length=255, blank=True, verbose_name='SEO H1')
    canonical_path = models.CharField(max_length=255, blank=True, verbose_name='Canonical path')
    og_title = models.CharField(max_length=255, blank=True, verbose_name='OG title')
    og_description = models.TextField(blank=True, verbose_name='OG description')
    og_image_url = models.URLField(blank=True, verbose_name='OG image URL')
    meta_robots = models.CharField(max_length=100, default='index,follow', verbose_name='Meta robots')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    is_system = models.BooleanField(default=False, verbose_name='Системный')
    sort_order = models.PositiveSmallIntegerField(default=100, verbose_name='Порядок')
    supports_auto_parts_enrichment = models.BooleanField(
        default=False, verbose_name='Разрешает обогащение автозапчастей',
    )
    requires_product_classification = models.BooleanField(
        default=False, verbose_name='Требует проверки домена товара',
    )

    class Meta:
        verbose_name = 'Домен каталога'
        verbose_name_plural = 'Домены каталога'
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active', 'sort_order']),
        ]

    def __str__(self):
        return self.name


class Tenant(TimestampedModel):
    """Организация-тенант. Единица изоляции данных в системе."""

    class CatalogDomain(models.TextChoices):
        AUTO_PARTS = 'auto_parts', 'Автозапчасти'
        MIXED = 'mixed', 'Авто-ти + Другие товары'
        GENERIC = 'generic', 'Смешанный каталог'
        JEWELLERY = 'jewellery', 'Украшения'
        APPAREL = 'apparel', 'Одежда'
        OTHER = 'other', 'Другое'
        UNKNOWN = 'unknown', 'Не определено'

    name = models.CharField(max_length=200, verbose_name='Название')
    slug = models.SlugField(unique=True, verbose_name='Slug')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    catalog_domain = models.CharField(
        max_length=50,
        default=CatalogDomain.UNKNOWN, verbose_name='Домен каталога',
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True, verbose_name='Окончание триала')

    # Кэш счётчиков — обновляется задачей update_tenant_counters
    active_listings_count = models.PositiveIntegerField(default=0, verbose_name='Активных листингов')
    sku_count = models.PositiveIntegerField(default=0, verbose_name='SKU (товаров)')
    ai_credits_used = models.PositiveIntegerField(default=0, verbose_name='Использовано AI-кредитов')
    ai_credit_limit_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='Индивидуальный месячный лимит AI-кредитов',
        help_text='Пусто — использовать лимит тарифного плана.',
    )

    class Meta:
        verbose_name = 'Тенант'
        verbose_name_plural = 'Тенанты'
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return self.name

    @property
    def supports_auto_parts_enrichment(self) -> bool:
        domain = CatalogDomain.objects.filter(slug=self.catalog_domain).first()
        if domain is not None:
            return domain.supports_auto_parts_enrichment
        return self.catalog_domain in [
            self.CatalogDomain.AUTO_PARTS,
            self.CatalogDomain.MIXED,
        ]

    @property
    def requires_product_auto_parts_check(self) -> bool:
        domain = CatalogDomain.objects.filter(slug=self.catalog_domain).first()
        if domain is not None:
            return domain.requires_product_classification
        return self.catalog_domain == self.CatalogDomain.MIXED


class TenantCatalogDomain(TimestampedModel):
    """Корневое направление каталога, включенное конкретному tenant-у."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='enabled_catalog_domains',
        verbose_name='Тенант',
    )
    domain = models.ForeignKey(
        CatalogDomain, on_delete=models.CASCADE, related_name='tenant_enablings',
        verbose_name='Корневая категория',
    )
    is_enabled = models.BooleanField(default=True, verbose_name='Включена')

    class Meta:
        verbose_name = 'Корневая категория tenant-а'
        verbose_name_plural = 'Корневые категории tenant-а'
        ordering = ['domain__sort_order', 'domain__name']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'domain'],
                name='unique_tenant_catalog_domain',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant', 'is_enabled']),
            models.Index(fields=['domain', 'is_enabled']),
        ]

    def __str__(self):
        return f'{self.tenant}: {self.domain}'


class TenantUser(TimestampedModel):
    """Пользователь в контексте тенанта с ролью."""

    ROLE_OWNER = 'owner'
    ROLE_ADMIN = 'admin'
    ROLE_OPERATOR = 'operator'
    ROLE_VIEWER = 'viewer'

    ROLES = [
        (ROLE_OWNER, 'Владелец'),
        (ROLE_ADMIN, 'Администратор'),
        (ROLE_OPERATOR, 'Оператор'),
        (ROLE_VIEWER, 'Наблюдатель'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tenant_memberships',
        verbose_name='Пользователь',
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='members',
        verbose_name='Тенант',
    )
    role = models.CharField(choices=ROLES, default=ROLE_OPERATOR, max_length=20, verbose_name='Роль')

    class Meta:
        verbose_name = 'Пользователь тенанта'
        verbose_name_plural = 'Пользователи тенантов'
        unique_together = [('user', 'tenant')]

    def __str__(self):
        return f'{self.user.email} @ {self.tenant.slug} ({self.role})'

    # Матрица прав
    def can_manage_billing(self):
        return self.role == self.ROLE_OWNER

    def can_manage_users(self):
        return self.role in (self.ROLE_OWNER, self.ROLE_ADMIN)

    def can_manage_connections(self):
        return self.role in (self.ROLE_OWNER, self.ROLE_ADMIN)

    def can_publish(self):
        return self.role in (self.ROLE_OWNER, self.ROLE_ADMIN, self.ROLE_OPERATOR)


class APIKey(TimestampedModel):
    """API-ключ для доступа к системе от имени тенанта."""

    KEY_PREFIX = 'map_sk_'

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='api_keys',
    )
    name = models.CharField(max_length=100, verbose_name='Название')
    key_prefix = models.CharField(max_length=12, verbose_name='Префикс ключа')
    key_hash = models.CharField(max_length=64, verbose_name='SHA256-хэш ключа')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее использование')

    class Meta:
        verbose_name = 'API-ключ'
        verbose_name_plural = 'API-ключи'
        indexes = [
            models.Index(fields=['key_hash']),
            models.Index(fields=['tenant', 'is_active']),
        ]

    def __str__(self):
        return f'{self.key_prefix}... ({self.tenant.slug})'

    @classmethod
    def generate(cls, tenant: 'Tenant', name: str) -> tuple['APIKey', str]:
        """
        Создаёт новый API-ключ.

        Возвращает (объект APIKey, plaintext-ключ).
        Plaintext показывается только один раз — потом доступен только хэш.
        """
        plaintext = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        key_prefix = plaintext[:12]

        api_key = cls.objects.create(
            tenant=tenant,
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
        return api_key, plaintext

    @classmethod
    def verify(cls, plaintext: str) -> 'APIKey | None':
        """Проверяет ключ по хэшу. Возвращает объект или None."""
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        return cls.objects.filter(key_hash=key_hash, is_active=True).select_related('tenant').first()


class WebhookEndpoint(TimestampedModel):
    """Вебхук-эндпоинт тенанта для получения событий системы."""

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='webhook_endpoints',
    )
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=64)  # HMAC-секрет в plaintext (генерируется один раз)
    events = models.JSONField(default=list)   # список событий, напр. ['listing.published']
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Вебхук-эндпоинт'
        verbose_name_plural = 'Вебхук-эндпоинты'
        indexes = [
            models.Index(fields=['tenant', 'is_active']),
        ]

    def __str__(self):
        return f'{self.tenant.slug}: {self.url}'

    @classmethod
    def generate_secret(cls) -> str:
        """Генерирует случайный HMAC-секрет для подписи вебхуков."""
        return secrets.token_hex(32)
