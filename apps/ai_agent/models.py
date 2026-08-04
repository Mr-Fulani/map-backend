from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from apps.ai_agent.provider_registry import provider_choices, provider_is_configured
from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class AITaskType(models.TextChoices):
    DESCRIPTION = 'description_generation', 'Генерация описаний'
    CLASSIFICATION = 'classification', 'Классификация товаров'
    ATTRIBUTE_EXTRACTION = 'attribute_extraction', 'Извлечение характеристик'
    FITMENT_RESOLUTION = 'fitment_resolution', 'Анализ совместимости'


class AIModel(TimestampedModel):
    """Разрешённая в MAP модель с внутренней ценой в AI-кредитах."""

    PROVIDER_OPENAI = 'openai'
    PROVIDER_ANTHROPIC = 'anthropic'
    PROVIDER_GEMINI = 'gemini'
    PROVIDER_DEEPSEEK = 'deepseek'
    PROVIDER_KIMI = 'kimi'
    PROVIDER_CHOICES = provider_choices()

    QUALITY_STANDARD = 'standard'
    QUALITY_ADVANCED = 'advanced'
    QUALITY_MAXIMUM = 'maximum'
    QUALITY_CHOICES = [
        (QUALITY_STANDARD, 'Стандарт'),
        (QUALITY_ADVANCED, 'Повышенное'),
        (QUALITY_MAXIMUM, 'Максимальное'),
    ]

    SPEED_FAST = 'fast'
    SPEED_BALANCED = 'balanced'
    SPEED_SLOW = 'slow'
    SPEED_CHOICES = [
        (SPEED_FAST, 'Быстрая'),
        (SPEED_BALANCED, 'Средняя'),
        (SPEED_SLOW, 'Медленная'),
    ]

    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    external_id = models.CharField(max_length=120, unique=True)
    display_name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    quality_tier = models.CharField(
        max_length=20, choices=QUALITY_CHOICES, default=QUALITY_STANDARD,
    )
    speed_tier = models.CharField(
        max_length=20, choices=SPEED_CHOICES, default=SPEED_BALANCED,
    )
    supported_tasks = models.JSONField(default=list)

    # Внутренняя цена уже включает курс, инфраструктурный запас и маржу.
    input_credits_per_million = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0'),
    )
    cached_input_credits_per_million = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0'),
    )
    output_credits_per_million = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0'),
    )
    minimum_credits = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('1'),
    )

    max_output_tokens = models.PositiveIntegerField(default=2048)
    reasoning_effort = models.CharField(max_length=20, blank=True, default='')
    is_active = models.BooleanField(default=True)
    is_pricing_verified = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    is_fallback = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        verbose_name = 'AI-модель'
        verbose_name_plural = 'AI-модели'
        ordering = ['sort_order', 'display_name']
        indexes = [
            models.Index(fields=['is_active', 'sort_order']),
            models.Index(fields=['provider', 'is_active']),
        ]

    def __str__(self):
        return f'{self.display_name} ({self.provider})'

    def supports_task(self, task_type: str) -> bool:
        return task_type in self.supported_tasks

    @property
    def is_configured(self) -> bool:
        return provider_is_configured(self.provider)

    @property
    def is_selectable(self) -> bool:
        return self.is_active and self.is_pricing_verified and self.is_configured

    @property
    def availability_reason(self) -> str:
        if not self.is_active:
            return 'Модель отключена администратором.'
        if not self.is_pricing_verified:
            return 'Стоимость провайдера ещё не подтверждена.'
        if not self.is_configured:
            return 'API-ключ провайдера не настроен.'
        return ''

    def calculate_credits(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> Decimal:
        uncached_input = max(0, input_tokens - cached_input_tokens)
        cost = (
            Decimal(uncached_input) * self.input_credits_per_million
            + Decimal(cached_input_tokens) * self.cached_input_credits_per_million
            + Decimal(output_tokens) * self.output_credits_per_million
        ) / Decimal('1000000')
        cost = max(cost, self.minimum_credits)
        return cost.quantize(Decimal('0.0001'), rounding=ROUND_UP)

    def estimate_credits(self, input_tokens: int, output_tokens: int | None = None) -> Decimal:
        return self.calculate_credits(
            input_tokens=input_tokens,
            output_tokens=output_tokens or self.max_output_tokens,
        )

    def provider_price_at(self, at=None):
        """Возвращает последнюю вступившую в силу цену провайдера."""
        return self.provider_prices.effective_at(at)


class AIProviderPriceQuerySet(models.QuerySet):
    def effective_at(self, at=None):
        at = at or timezone.now()
        return self.filter(effective_from__lte=at).order_by(
            '-effective_from', '-pk',
        ).first()

    def update(self, **kwargs):
        raise ValidationError(
            'Версии цен неизменяемы. Создайте новую запись с новой датой.',
        )

    def delete(self):
        raise ValidationError('Исторические версии цен нельзя удалять.')


class AIProviderPrice(models.Model):
    """Неизменяемая версия фактической цены AI-провайдера за 1 млн токенов."""

    model = models.ForeignKey(
        AIModel,
        on_delete=models.PROTECT,
        related_name='provider_prices',
    )
    currency = models.CharField(
        max_length=3,
        default='USD',
        validators=[
            RegexValidator(
                regex=r'^[A-Z]{3}$',
                message='Валюта должна быть трёхбуквенным кодом ISO 4217.',
            ),
        ],
    )
    input_per_million = models.DecimalField(
        max_digits=20, decimal_places=8, default=Decimal('0'),
    )
    cached_read_per_million = models.DecimalField(
        max_digits=20, decimal_places=8, default=Decimal('0'),
    )
    cached_write_per_million = models.DecimalField(
        max_digits=20, decimal_places=8, default=Decimal('0'),
    )
    output_per_million = models.DecimalField(
        max_digits=20, decimal_places=8, default=Decimal('0'),
    )
    effective_from = models.DateTimeField(db_index=True)
    source_url = models.URLField(blank=True)
    notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AIProviderPriceQuerySet.as_manager()

    class Meta:
        verbose_name = 'Цена AI-провайдера'
        verbose_name_plural = 'Цены AI-провайдеров'
        ordering = ['-effective_from', '-pk']
        constraints = [
            models.UniqueConstraint(
                fields=['model', 'effective_from'],
                name='unique_ai_model_price_effective_from',
            ),
            models.CheckConstraint(
                check=models.Q(input_per_million__gte=0),
                name='ai_price_input_nonnegative',
            ),
            models.CheckConstraint(
                check=models.Q(cached_read_per_million__gte=0),
                name='ai_price_cached_read_nonnegative',
            ),
            models.CheckConstraint(
                check=models.Q(cached_write_per_million__gte=0),
                name='ai_price_cached_write_nonnegative',
            ),
            models.CheckConstraint(
                check=models.Q(output_per_million__gte=0),
                name='ai_price_output_nonnegative',
            ),
        ]
        indexes = [
            models.Index(
                fields=['model', '-effective_from'],
                name='ai_price_model_effective_idx',
            ),
            models.Index(fields=['currency'], name='ai_price_currency_idx'),
        ]

    def __str__(self):
        return (
            f'{self.model.external_id}: {self.currency}, '
            f'с {self.effective_from.isoformat()}'
        )

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(
                'Версия цены неизменяема. Создайте новую запись с новой датой.',
            )
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Исторические версии цен нельзя удалять.')

    def calculate_cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_read_tokens: int = 0,
        cached_write_tokens: int = 0,
    ) -> Decimal:
        total_input_tokens = max(0, input_tokens)
        cached_read_tokens = max(
            0, min(total_input_tokens, cached_read_tokens),
        )
        cached_write_tokens = max(
            0,
            min(
                total_input_tokens - cached_read_tokens,
                cached_write_tokens,
            ),
        )
        uncached_input_tokens = (
            total_input_tokens - cached_read_tokens - cached_write_tokens
        )
        cost = (
            Decimal(uncached_input_tokens) * self.input_per_million
            + Decimal(cached_read_tokens) * self.cached_read_per_million
            + Decimal(cached_write_tokens) * self.cached_write_per_million
            + Decimal(max(0, output_tokens)) * self.output_per_million
        ) / Decimal('1000000')
        return cost.quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)


class TenantAISettings(TimestampedModel):
    """Модель по умолчанию для всех AI-задач тенанта."""

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name='ai_settings',
    )
    default_model = models.ForeignKey(
        AIModel,
        on_delete=models.PROTECT,
        related_name='default_for_tenants',
        null=True,
        blank=True,
    )
    use_task_overrides = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Настройки AI тенанта'
        verbose_name_plural = 'Настройки AI тенантов'

    def __str__(self):
        return f'AI-настройки {self.tenant.slug}'


class TenantAITaskModel(TimestampedModel):
    """Необязательный выбор модели для конкретной задачи."""

    settings = models.ForeignKey(
        TenantAISettings, on_delete=models.CASCADE, related_name='task_models',
    )
    task_type = models.CharField(max_length=40, choices=AITaskType.choices)
    model = models.ForeignKey(
        AIModel, on_delete=models.PROTECT, related_name='task_overrides',
    )

    class Meta:
        verbose_name = 'Модель AI-задачи'
        verbose_name_plural = 'Модели AI-задач'
        constraints = [
            models.UniqueConstraint(
                fields=['settings', 'task_type'],
                name='unique_tenant_ai_task_model',
            ),
        ]


class AIRequestLog(TimestampedModel):
    """Фактическое использование модели и списание с тенанта."""

    STATUS_SUCCESS = 'success'
    STATUS_ERROR = 'error'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_SUCCESS, 'Успешно'),
        (STATUS_ERROR, 'Ошибка провайдера'),
        (STATUS_REJECTED, 'Ответ отклонён валидатором'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='ai_request_logs',
    )
    task_type = models.CharField(max_length=40, choices=AITaskType.choices)
    provider = models.CharField(max_length=20)
    model_id = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    input_tokens = models.PositiveIntegerField(default=0)
    cached_input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    charged_credits = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0'),
    )
    duration_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = 'AI-запрос'
        verbose_name_plural = 'AI-запросы'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['tenant', 'task_type', '-created_at']),
            models.Index(fields=['provider', 'model_id', '-created_at']),
        ]
