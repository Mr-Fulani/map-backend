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


class Tenant(TimestampedModel):
    """Организация-тенант. Единица изоляции данных в системе."""

    name = models.CharField(max_length=200, verbose_name='Название')
    slug = models.SlugField(unique=True, verbose_name='Slug')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    trial_ends_at = models.DateTimeField(null=True, blank=True, verbose_name='Окончание триала')

    # Кэш счётчиков — обновляется задачей update_tenant_counters
    active_listings_count = models.PositiveIntegerField(default=0, verbose_name='Активных листингов')
    sku_count = models.PositiveIntegerField(default=0, verbose_name='SKU (товаров)')
    ai_credits_used = models.PositiveIntegerField(default=0, verbose_name='Использовано AI-кредитов')

    class Meta:
        verbose_name = 'Тенант'
        verbose_name_plural = 'Тенанты'
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return self.name


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
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='members',
    )
    role = models.CharField(choices=ROLES, default=ROLE_OPERATOR, max_length=20)

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
