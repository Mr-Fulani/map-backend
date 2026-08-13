import uuid

from django.db import models
from django.utils import timezone

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class ImageSearchLog(TimestampedModel):
    """Лог каждого запроса к источнику изображений.

    Хранит информацию о запросе, результатах и ошибках.
    Используется для мониторинга hit rate и здоровья источников.
    """

    class Outcome(models.TextChoices):
        UNKNOWN = 'unknown', 'Не классифицировано (legacy)'
        COMPLETED = 'completed', 'Завершено'
        SAFE_FAILURE = 'safe_failure', 'Безопасный отказ'
        OUTCOME_UNCERTAIN = 'outcome_uncertain', 'Результат провайдера неизвестен'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='image_search_logs', verbose_name='Тенант',
    )
    product = models.ForeignKey(
        'products.Product', on_delete=models.CASCADE,
        related_name='image_search_logs', verbose_name='Товар',
    )
    source_id = models.CharField(max_length=50, verbose_name='Источник')
    query = models.CharField(max_length=500, verbose_name='Поисковый запрос')
    confidence = models.CharField(max_length=10, blank=True, verbose_name='Уверенность запроса')
    results_count = models.PositiveIntegerField(default=0, verbose_name='Найдено кандидатов')
    accepted_count = models.PositiveSmallIntegerField(default=0, verbose_name='Принято')
    duration_ms = models.PositiveIntegerField(default=0, verbose_name='Длительность (мс)')
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        default=Outcome.UNKNOWN,
        verbose_name='Результат запроса к провайдеру',
    )
    error_code = models.CharField(
        max_length=80,
        blank=True,
        verbose_name='Код ошибки',
    )
    error = models.TextField(blank=True, verbose_name='Ошибка')
    query_metrics = models.JSONField(
        default=list, blank=True, verbose_name='Метрики запросов',
    )
    query_builder_version = models.CharField(
        max_length=20, blank=True, verbose_name='Версия построителя запросов',
    )
    workflow_key = models.CharField(
        max_length=160,
        blank=True,
        editable=False,
        verbose_name='Ключ durable workflow',
    )
    workflow_slot = models.CharField(
        max_length=160,
        blank=True,
        editable=False,
        verbose_name='Логический слот durable workflow',
    )

    class Meta:
        verbose_name = 'Лог поиска изображений'
        verbose_name_plural = 'Логи поиска изображений'
        indexes = [
            models.Index(fields=['created_at'], name='img_log_created_idx'),
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['product', 'created_at']),
            models.Index(fields=['source_id', 'created_at']),
            models.Index(
                fields=['workflow_key', 'workflow_slot'],
                name='img_log_workflow_slot_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['workflow_key', 'workflow_slot'],
                condition=~models.Q(workflow_key=''),
                name='uniq_image_log_workflow_slot',
            ),
        ]

    def __str__(self):
        return f'[{self.source_id}] {self.product_id} — {self.results_count} results'


class ImageSearchIntent(TimestampedModel):
    """Canonical tenant request owning one single or bulk search submission."""

    class Operation(models.TextChoices):
        SINGLE = 'single', 'Одиночный поиск'
        BULK = 'bulk', 'Массовый поиск'

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='image_search_intents',
    )
    operation = models.CharField(max_length=20, choices=Operation.choices)
    idempotency_key = models.UUIDField(default=uuid.uuid4, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    request_payload = models.JSONField(default=dict, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'operation', 'idempotency_key'],
                name='unique_tenant_image_search_intent',
            ),
        ]
        indexes = [
            models.Index(
                fields=['tenant', '-created_at'],
                name='img_intent_tenant_created_idx',
            ),
        ]


class ImageSearchTask(TimestampedModel):
    """Tenant/product ownership record for a durable image-search result."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает'
        RUNNING = 'running', 'В работе'
        SUCCEEDED = 'succeeded', 'Завершено'
        FAILED = 'failed', 'Ошибка'
        RECONCILIATION_REQUIRED = (
            'reconciliation_required',
            'Требует сверки провайдера',
        )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='image_search_tasks',
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='image_search_tasks',
    )
    task_id = models.CharField(max_length=255, unique=True)
    dispatch = models.ForeignKey(
        'core.BackgroundJobDispatch',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='image_search_requests',
    )
    intent = models.ForeignKey(
        ImageSearchIntent,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='tasks',
    )
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    result = models.JSONField(null=True, blank=True, editable=False)
    error_code = models.CharField(max_length=80, blank=True, editable=False)
    error_message = models.CharField(max_length=500, blank=True, editable=False)
    finished_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=['created_at'], name='img_task_created_idx'),
            models.Index(
                fields=['tenant', '-created_at'],
                name='img_task_tenant_created_idx',
            ),
            models.Index(
                fields=['product', '-created_at'],
                name='img_task_product_created_idx',
            ),
            models.Index(
                fields=['status', '-updated_at'],
                name='img_task_status_updated_idx',
            ),
        ]

    def __str__(self):
        return f'{self.tenant_id}:{self.product_id}:{self.task_id}'


class ImageSearchCache(TimestampedModel):
    """Кеш результатов поиска по артикулу + бренду.

    Позволяет не дёргать источники повторно для того же товара.
    TTL задаётся через IMAGE_SEARCH_SETTINGS['CACHE_TTL_DAYS'].
    Инвалидируется при ручном approve/reject модератором.
    """

    cache_key = models.CharField(
        max_length=200, unique=True, db_index=True,
        verbose_name='Ключ кеша',
        help_text='Формат: img_search:{article}:{brand}',
    )
    results = models.JSONField(verbose_name='Результаты (JSON)')
    expires_at = models.DateTimeField(verbose_name='Истекает')

    class Meta:
        verbose_name = 'Кеш поиска изображений'
        verbose_name_plural = 'Кеш поиска изображений'
        indexes = [
            models.Index(fields=['expires_at'], name='img_cache_expires_idx'),
        ]

    def __str__(self):
        return self.cache_key


class BraveQuota(models.Model):
    """Персистентный счётчик запросов к Brave Search API за расчётный период (YYYY-MM).

    Singleton-per-period: одна запись на календарный месяц.
    Счётчик атомарно инкрементируется через F() при каждом HTTP-запросе к Brave.
    Soft cap = 800 — после него is_available() возвращает False до конца месяца
    либо до пополнения баланса и сброса вручную.
    """

    SOFT_CAP = 800
    DEFAULT_LIMIT = 1000

    period = models.CharField(
        max_length=7, unique=True, db_index=True,
        verbose_name='Период (YYYY-MM)',
    )
    requests_used = models.PositiveIntegerField(
        default=0, verbose_name='Использовано запросов',
    )
    limit = models.PositiveIntegerField(
        default=DEFAULT_LIMIT, verbose_name='Лимит по плану',
    )
    cap_notified = models.BooleanField(
        default=False, verbose_name='Уведомление об исчерпании отправлено',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Квота Brave Search'
        verbose_name_plural = 'Квота Brave Search'

    def __str__(self):
        return f'Brave {self.period}: {self.requests_used}/{self.limit}'

    @classmethod
    def current(cls) -> 'BraveQuota':
        """Возвращает (или создаёт) запись за текущий месяц."""
        period = timezone.now().strftime('%Y-%m')
        obj, _ = cls.objects.get_or_create(period=period)
        return obj

    @classmethod
    def increment(cls) -> 'BraveQuota':
        """Атомарно инкрементирует счётчик текущего месяца, возвращает обновлённый объект."""
        period = timezone.now().strftime('%Y-%m')
        cls.objects.get_or_create(period=period)
        cls.objects.filter(period=period).update(
            requests_used=models.F('requests_used') + 1,
        )
        return cls.objects.get(period=period)

    @classmethod
    def is_soft_cap_reached(cls) -> bool:
        """True если в текущем месяце использовано >= SOFT_CAP запросов."""
        period = timezone.now().strftime('%Y-%m')
        quota = cls.objects.filter(period=period).first()
        return quota is not None and quota.requests_used >= cls.SOFT_CAP
