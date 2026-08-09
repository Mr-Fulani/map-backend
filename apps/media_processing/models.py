import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.core.models import TimestampedModel
from apps.media_processing.providers.base import MediaOperation


def _validate_operations(value) -> None:
    if not isinstance(value, list) or not value:
        raise ValidationError('Укажите хотя бы одну операцию обработки.')
    try:
        normalized = [MediaOperation(operation).value for operation in value]
    except (TypeError, ValueError) as exc:
        raise ValidationError(f'Неизвестная операция обработки: {exc}') from exc
    if len(normalized) != len(set(normalized)):
        raise ValidationError('Операции обработки не должны повторяться.')


class MediaProviderPolicy(TimestampedModel):
    """Non-secret routing and tariff policy for a registered provider adapter."""

    provider_id = models.SlugField(max_length=50, unique=True, verbose_name='Провайдер')
    display_name = models.CharField(max_length=120, verbose_name='Название')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    priority = models.PositiveSmallIntegerField(default=100, verbose_name='Приоритет')
    capabilities = models.JSONField(default=list, verbose_name='Возможности')
    allowed_plan_slugs = models.JSONField(
        default=list, blank=True, verbose_name='Доступен тарифам',
        help_text='Пустой список — доступен всем тарифам.',
    )
    operation_credit_costs = models.JSONField(
        default=dict, blank=True, verbose_name='Стоимость операций в AI-кредитах',
    )
    requests_per_minute = models.PositiveIntegerField(
        default=60, verbose_name='Лимит запросов в минуту',
    )
    notes = models.TextField(blank=True, verbose_name='Примечание')

    class Meta:
        verbose_name = 'Политика медиа-провайдера'
        verbose_name_plural = 'Политики медиа-провайдеров'
        ordering = ['priority', 'display_name']

    def __str__(self):
        return self.display_name

    def clean(self):
        super().clean()
        if self.capabilities:
            _validate_operations(self.capabilities)
        invalid_costs = set(self.operation_credit_costs) - {
            operation.value for operation in MediaOperation
        }
        if invalid_costs:
            raise ValidationError({
                'operation_credit_costs': (
                    'Неизвестные операции: ' + ', '.join(sorted(invalid_costs))
                ),
            })
        missing_costs = set(self.capabilities or []) - set(self.operation_credit_costs or {})
        if missing_costs:
            raise ValidationError({
                'operation_credit_costs': (
                    'Явно укажите стоимость, включая 0 для бесплатных операций: '
                    + ', '.join(sorted(missing_costs))
                ),
            })
        invalid_values = []
        for operation, raw_cost in (self.operation_credit_costs or {}).items():
            try:
                cost = Decimal(str(raw_cost))
            except (ArithmeticError, TypeError, ValueError):
                invalid_values.append(operation)
                continue
            if not cost.is_finite() or cost < 0:
                invalid_values.append(operation)
        if invalid_values:
            raise ValidationError({
                'operation_credit_costs': (
                    'Стоимость должна быть конечным неотрицательным числом: '
                    + ', '.join(sorted(invalid_values))
                ),
            })


class MediaProcessingPreset(TimestampedModel):
    """Reusable operation sequence for a tenant or the whole platform."""

    tenant = models.ForeignKey(
        'tenants.Tenant', null=True, blank=True, on_delete=models.CASCADE,
        related_name='media_processing_presets', verbose_name='Тенант',
    )
    name = models.CharField(max_length=120, verbose_name='Название')
    slug = models.SlugField(max_length=80, verbose_name='Slug')
    operations = models.JSONField(
        default=list, validators=[_validate_operations], verbose_name='Операции',
    )
    parameters = models.JSONField(default=dict, blank=True, verbose_name='Параметры')
    provider_preferences = models.JSONField(
        default=list, blank=True, verbose_name='Приоритет провайдеров',
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    is_default = models.BooleanField(default=False, verbose_name='По умолчанию')

    class Meta:
        verbose_name = 'Пресет обработки медиа'
        verbose_name_plural = 'Пресеты обработки медиа'
        ordering = ['tenant_id', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'slug'],
                condition=Q(tenant__isnull=False),
                name='unique_tenant_media_preset_slug',
            ),
            models.UniqueConstraint(
                fields=['slug'],
                condition=Q(tenant__isnull=True),
                name='unique_platform_media_preset_slug',
            ),
            models.UniqueConstraint(
                fields=['tenant'],
                condition=Q(is_default=True, tenant__isnull=False),
                name='unique_default_tenant_media_preset',
            ),
            models.UniqueConstraint(
                fields=['is_default'],
                condition=Q(is_default=True, tenant__isnull=True),
                name='unique_default_platform_media_preset',
            ),
        ]

    def __str__(self):
        scope = self.tenant.slug if self.tenant_id else 'platform'
        return f'{self.name} ({scope})'


class TenantMediaSettings(TimestampedModel):
    """Tenant defaults; provider preferences can later be derived from its plan."""

    tenant = models.OneToOneField(
        'tenants.Tenant', on_delete=models.CASCADE,
        related_name='media_settings', verbose_name='Тенант',
    )
    default_preset = models.ForeignKey(
        MediaProcessingPreset, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='default_for_tenants', verbose_name='Пресет по умолчанию',
    )
    provider_preferences = models.JSONField(
        default=dict, blank=True, verbose_name='Приоритеты провайдеров',
        help_text='Ключ — операция или *, значение — список provider_id.',
    )
    auto_process_manual_uploads = models.BooleanField(
        default=False, verbose_name='Автообработка ручных загрузок',
    )
    auto_process_approved_search = models.BooleanField(
        default=False, verbose_name='Автообработка одобренных результатов поиска',
    )
    allow_generative_operations = models.BooleanField(
        default=False, verbose_name='Разрешить генеративные операции',
    )

    class Meta:
        verbose_name = 'Настройки медиа тенанта'
        verbose_name_plural = 'Настройки медиа тенантов'

    def __str__(self):
        return f'Медиа — {self.tenant}'


class MediaProcessingJob(TimestampedModel):
    """Auditable provider-neutral processing request."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'В очереди'
        SUBMITTED = 'submitted', 'Передано провайдеру'
        PROCESSING = 'processing', 'Обрабатывается'
        SUCCEEDED = 'succeeded', 'Готово'
        FAILED = 'failed', 'Ошибка'
        CANCELLED = 'cancelled', 'Отменено'

    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE,
        related_name='media_processing_jobs', verbose_name='Тенант',
    )
    product_image = models.ForeignKey(
        'products.ProductImage', on_delete=models.CASCADE,
        related_name='processing_jobs', verbose_name='Исходное изображение',
    )
    preset = models.ForeignKey(
        MediaProcessingPreset, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='jobs', verbose_name='Пресет',
    )
    operations = models.JSONField(
        default=list, validators=[_validate_operations], verbose_name='Операции',
    )
    parameters = models.JSONField(default=dict, blank=True, verbose_name='Параметры')
    provider_id = models.SlugField(max_length=50, blank=True, verbose_name='Провайдер')
    provider_job_id = models.CharField(
        max_length=255, blank=True, db_index=True, verbose_name='ID задачи провайдера',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED,
        db_index=True, verbose_name='Статус',
    )
    idempotency_key = models.CharField(
        max_length=64, default=uuid.uuid4, editable=False,
        verbose_name='Ключ идемпотентности',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='requested_media_jobs', verbose_name='Запустил',
    )
    provider_metadata = models.JSONField(
        default=dict, blank=True, verbose_name='Метаданные провайдера',
    )
    estimated_credits = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('0'),
        verbose_name='Оценка кредитов',
    )
    charged_credits = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('0'),
        verbose_name='Списано кредитов',
    )
    error_code = models.CharField(max_length=100, blank=True, verbose_name='Код ошибки')
    error_message = models.TextField(blank=True, verbose_name='Ошибка')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='Начато')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='Завершено')

    class Meta:
        verbose_name = 'Задача обработки медиа'
        verbose_name_plural = 'Задачи обработки медиа'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'status', '-created_at']),
            models.Index(fields=['product_image', '-created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'idempotency_key'],
                name='unique_tenant_media_job_idempotency',
            ),
        ]

    def __str__(self):
        return f'Media job #{self.pk} — {self.get_status_display()}'


class ProductImageVariant(TimestampedModel):
    """Immutable derived file produced from one ProductImage original."""

    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE,
        related_name='product_image_variants', verbose_name='Тенант',
    )
    product_image = models.ForeignKey(
        'products.ProductImage', on_delete=models.CASCADE,
        related_name='variants', verbose_name='Исходное изображение',
    )
    job = models.ForeignKey(
        MediaProcessingJob, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='variants', verbose_name='Задача',
    )
    provider_id = models.SlugField(max_length=50, blank=True, verbose_name='Провайдер')
    operations = models.JSONField(default=list, blank=True, verbose_name='Операции')
    parameters = models.JSONField(default=dict, blank=True, verbose_name='Параметры')
    s3_key = models.CharField(max_length=500, verbose_name='Ключ S3')
    content_type = models.CharField(
        max_length=100, default='image/jpeg', verbose_name='Content-Type',
    )
    width = models.PositiveIntegerField(null=True, blank=True, verbose_name='Ширина')
    height = models.PositiveIntegerField(null=True, blank=True, verbose_name='Высота')
    file_size_kb = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Размер файла (KB)',
    )
    sha256 = models.CharField(max_length=64, db_index=True, verbose_name='SHA256')
    is_active = models.BooleanField(default=False, verbose_name='Используется для публикации')

    class Meta:
        verbose_name = 'Вариант изображения товара'
        verbose_name_plural = 'Варианты изображений товаров'
        ordering = ['-is_active', '-created_at']
        indexes = [models.Index(fields=['tenant', '-created_at'])]
        constraints = [
            models.UniqueConstraint(
                fields=['product_image', 'sha256'],
                name='unique_product_image_variant_sha',
            ),
            models.UniqueConstraint(
                fields=['product_image'],
                condition=Q(is_active=True),
                name='unique_active_product_image_variant',
            ),
        ]

    def __str__(self):
        return f'Variant #{self.pk} for image #{self.product_image_id}'


class ImageAssessment(TimestampedModel):
    """Explainable technical or semantic assessment of a candidate/product image."""

    class Verdict(models.TextChoices):
        ACCEPT = 'accept', 'Подходит'
        REVIEW = 'review', 'Нужна проверка'
        REJECT = 'reject', 'Не подходит'
        ERROR = 'error', 'Ошибка проверки'

    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE,
        related_name='image_assessments', verbose_name='Тенант',
    )
    product = models.ForeignKey(
        'products.Product', on_delete=models.CASCADE,
        related_name='image_assessments', verbose_name='Товар',
    )
    product_image = models.ForeignKey(
        'products.ProductImage', null=True, blank=True, on_delete=models.CASCADE,
        related_name='assessments', verbose_name='Изображение товара',
    )
    source_url = models.URLField(max_length=2000, blank=True, verbose_name='URL кандидата')
    source_id = models.CharField(max_length=50, blank=True, verbose_name='Источник')
    provider_id = models.SlugField(max_length=50, blank=True, verbose_name='Провайдер проверки')
    model_id = models.CharField(max_length=150, blank=True, verbose_name='Модель/версия')
    verdict = models.CharField(
        max_length=20, choices=Verdict.choices, default=Verdict.REVIEW,
        db_index=True, verbose_name='Решение',
    )
    score = models.FloatField(null=True, blank=True, verbose_name='Оценка 0–1')
    reason_codes = models.JSONField(default=list, blank=True, verbose_name='Причины')
    checks = models.JSONField(default=dict, blank=True, verbose_name='Проверки')
    expected_product = models.JSONField(
        default=dict, blank=True, verbose_name='Ожидаемые данные товара',
    )
    raw_response = models.JSONField(default=dict, blank=True, verbose_name='Ответ провайдера')

    class Meta:
        verbose_name = 'Проверка изображения'
        verbose_name_plural = 'Проверки изображений'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'verdict', '-created_at']),
            models.Index(fields=['product', '-created_at']),
        ]

    def __str__(self):
        return f'Assessment #{self.pk} — {self.get_verdict_display()}'
