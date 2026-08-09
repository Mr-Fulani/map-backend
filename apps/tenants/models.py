import hashlib
import re
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import SoftDeleteModel, TimestampedModel

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


API_KEY_SCOPE_CHOICES = (
    ('tenant:read', 'Tenant: read'),
    ('catalog:read', 'Catalog: read'),
    ('catalog:write', 'Catalog: write'),
    ('listings:read', 'Listings: read'),
    ('listings:write', 'Listings: write'),
    ('sync:read', 'Sync: read'),
    ('sync:run', 'Sync: run'),
    ('media:read', 'Media: read'),
    ('media:write', 'Media: write'),
    ('research:read', 'Research: read'),
    ('research:run', 'Research: run'),
    ('ai:read', 'AI: read'),
    ('ai:run', 'AI: run'),
)
API_KEY_SCOPES = frozenset(value for value, _ in API_KEY_SCOPE_CHOICES)
API_KEY_WRITE_SCOPES = frozenset(
    value for value in API_KEY_SCOPES
    if value.endswith((':write', ':run'))
)


def default_api_key_scopes() -> list[str]:
    return ['tenant:read']


def default_api_key_expiry():
    return timezone.now() + timedelta(days=90)


class APIKey(TimestampedModel):
    """API-ключ для доступа к системе от имени тенанта."""

    KEY_PREFIX = 'map_sk_'
    KEY_PAYLOAD_PATTERN = re.compile(r'^[A-Za-z0-9_-]{43}$')

    ROLE_VIEWER = 'viewer'
    ROLE_OPERATOR = 'operator'
    ROLE_CHOICES = (
        (ROLE_VIEWER, 'Viewer'),
        (ROLE_OPERATOR, 'Operator'),
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='api_keys',
    )
    name = models.CharField(max_length=100, verbose_name='Название')
    key_prefix = models.CharField(max_length=12, verbose_name='Префикс ключа')
    key_hash = models.CharField(
        max_length=64,
        unique=True,
        verbose_name='SHA256-хэш ключа',
    )
    role = models.CharField(
        max_length=16,
        choices=ROLE_CHOICES,
        default=ROLE_VIEWER,
        verbose_name='Роль интеграции',
    )
    scopes = models.JSONField(default=default_api_key_scopes, verbose_name='Scopes')
    expires_at = models.DateTimeField(
        default=default_api_key_expiry,
        db_index=True,
        verbose_name='Действует до',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_api_keys',
        verbose_name='Создал',
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name='Отозван')
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='revoked_api_keys',
        verbose_name='Отозвал',
    )
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее использование')

    class Meta:
        verbose_name = 'API-ключ'
        verbose_name_plural = 'API-ключи'
        indexes = [
            models.Index(fields=['tenant', 'is_active']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=['viewer', 'operator']),
                name='api_key_limited_role',
            ),
            models.CheckConstraint(
                condition=Q(is_active=False) | Q(revoked_at__isnull=True),
                name='active_api_key_not_revoked',
            ),
        ]

    def __str__(self):
        return f'{self.key_prefix}... ({self.tenant.slug})'

    @classmethod
    def generate(
        cls,
        tenant: 'Tenant',
        name: str,
        *,
        role: str = ROLE_VIEWER,
        scopes: list[str] | tuple[str, ...] | None = None,
        expires_at=None,
        created_by=None,
    ) -> tuple['APIKey', str]:
        """
        Создаёт новый API-ключ.

        Возвращает (объект APIKey, plaintext-ключ).
        Plaintext показывается только один раз — потом доступен только хэш.
        """
        normalized_scopes = sorted(set(
            default_api_key_scopes() if scopes is None else scopes
        ))
        unknown_scopes = set(normalized_scopes) - API_KEY_SCOPES
        if unknown_scopes:
            raise ValueError('Unknown API key scopes')
        if role not in {cls.ROLE_VIEWER, cls.ROLE_OPERATOR}:
            raise ValueError('API key role must be viewer or operator')
        if role == cls.ROLE_VIEWER and API_KEY_WRITE_SCOPES.intersection(
            normalized_scopes
        ):
            raise ValueError('Viewer API key cannot receive write scopes')

        expires_at = expires_at or default_api_key_expiry()
        current_time = timezone.now()
        if expires_at <= current_time:
            raise ValueError('API key expiry must be in the future')
        if expires_at > current_time + timedelta(days=365):
            raise ValueError('API key expiry cannot exceed 365 days')

        plaintext = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        key_prefix = plaintext[:12]

        api_key = cls.objects.create(
            tenant=tenant,
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            role=role,
            scopes=normalized_scopes,
            expires_at=expires_at,
            created_by=created_by,
        )
        return api_key, plaintext

    @classmethod
    def verify(cls, plaintext: str) -> 'APIKey | None':
        """Проверяет ключ по хэшу. Возвращает объект или None."""
        if not isinstance(plaintext, str) or not plaintext.startswith(cls.KEY_PREFIX):
            return None
        payload = plaintext[len(cls.KEY_PREFIX):]
        if not cls.KEY_PAYLOAD_PATTERN.fullmatch(payload):
            return None
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        return cls.objects.filter(
            key_hash=key_hash,
            is_active=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
            tenant__is_active=True,
        ).select_related('tenant').first()


class WebhookEndpoint(SoftDeleteModel):
    """Вебхук-эндпоинт тенанта для получения событий системы."""

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='webhook_endpoints',
    )
    url = models.URLField(max_length=500)
    secret_encrypted = models.BinaryField()  # Fernet, расшифровывается только перед подписью
    events = models.JSONField(default=list)   # список событий, напр. ['listing.published']
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Вебхук-эндпоинт'
        verbose_name_plural = 'Вебхук-эндпоинты'
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(deleted_at__isnull=False)
                    | Q(url__startswith='https://')
                ),
                name='webhook_endpoint_live_https_only',
            ),
            models.UniqueConstraint(
                fields=['tenant', 'url'],
                condition=Q(deleted_at__isnull=True),
                name='unique_live_tenant_webhook_url',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant', 'is_active']),
        ]

    def __str__(self):
        return f'{self.tenant.slug}: {self.url}'

    @classmethod
    def generate_secret(cls) -> str:
        """Генерирует случайный HMAC-секрет для подписи вебхуков."""
        return secrets.token_hex(32)

    def set_secret(self, value: str) -> None:
        from apps.datasources.encryption import encrypt_text
        self.secret_encrypted = encrypt_text(value)

    def get_secret(self) -> str:
        from apps.datasources.encryption import decrypt_text
        return decrypt_text(self.secret_encrypted)

    def soft_delete(self):
        """Disable and hide the endpoint atomically, releasing its URL."""
        if self.deleted_at is not None:
            return
        deleted_at = timezone.now()
        type(self).all_objects.filter(
            pk=self.pk,
            deleted_at__isnull=True,
        ).update(
            is_active=False,
            deleted_at=deleted_at,
            updated_at=deleted_at,
        )
        self.is_active = False
        self.deleted_at = deleted_at
        self.updated_at = deleted_at


class WebhookEvent(TimestampedModel):
    """Неизменяемое бизнес-событие transactional outbox."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='webhook_events_outbox',
    )
    event_type = models.CharField(max_length=64, choices=[(item, item) for item in WEBHOOK_EVENTS])
    payload = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Исходящее webhook-событие'
        verbose_name_plural = 'Исходящие webhook-события'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='unique_tenant_webhook_event_idempotency_key',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant', '-created_at'], name='wh_event_tenant_created_idx'),
        ]


class WebhookDelivery(TimestampedModel):
    """Состояние доставки одного outbox-события на один endpoint."""

    STATUS_PENDING = 'pending'
    STATUS_QUEUED = 'queued'
    STATUS_DELIVERING = 'delivering'
    STATUS_RETRY = 'retry'
    STATUS_DELIVERED = 'delivered'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Ожидает'),
        (STATUS_QUEUED, 'В очереди'),
        (STATUS_DELIVERING, 'Отправляется'),
        (STATUS_RETRY, 'Повтор'),
        (STATUS_DELIVERED, 'Доставлено'),
        (STATUS_FAILED, 'Не доставлено'),
    ]

    event = models.ForeignKey(
        WebhookEvent,
        on_delete=models.CASCADE,
        related_name='deliveries',
    )
    endpoint = models.ForeignKey(
        WebhookEndpoint,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deliveries',
    )
    endpoint_url = models.URLField(max_length=500)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=8)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    claimed_at = models.DateTimeField(null=True, blank=True, editable=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Доставка webhook'
        verbose_name_plural = 'Доставки webhook'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'endpoint'],
                name='unique_webhook_event_endpoint_delivery',
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status__in=['queued', 'delivering'],
                        claim_token__isnull=False,
                        claimed_at__isnull=False,
                    )
                    | Q(
                        status__in=[
                            'pending',
                            'retry',
                            'delivered',
                            'failed',
                        ],
                        claim_token__isnull=True,
                        claimed_at__isnull=True,
                    )
                ),
                name='webhook_delivery_claim_state_valid',
            ),
        ]
        indexes = [
            models.Index(
                fields=['status', 'next_attempt_at'],
                name='wh_delivery_status_due_idx',
            ),
            models.Index(
                fields=['endpoint', '-created_at'],
                name='wh_delivery_endpoint_idx',
            ),
            models.Index(
                fields=['status', 'claimed_at'],
                name='wh_delivery_claim_idx',
            ),
        ]
