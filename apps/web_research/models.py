from django.db import models

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class WebResearchRun(TimestampedModel):
    """Auditable product-research run, independent from concrete providers."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'В очереди'
        RUNNING = 'running', 'Выполняется'
        NEED_REVIEW = 'need_review', 'Нужна проверка'
        NO_RESULTS = 'no_results', 'Ничего не найдено'
        SKIPPED = 'skipped', 'Не требуется'
        FAILED = 'failed', 'Ошибка'

    class Trigger(models.TextChoices):
        MANUAL = 'manual', 'Вручную'
        PARSER_FALLBACK = 'parser_fallback', 'Fallback после каталогов'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='web_research_runs', verbose_name='Тенант',
    )
    product = models.ForeignKey(
        'products.Product', on_delete=models.CASCADE,
        related_name='web_research_runs', verbose_name='Товар',
    )
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.QUEUED,
        db_index=True, verbose_name='Статус',
    )
    trigger = models.CharField(
        max_length=30, choices=Trigger.choices, default=Trigger.MANUAL,
        verbose_name='Причина запуска',
    )
    search_provider = models.SlugField(
        max_length=50, blank=True, verbose_name='Провайдер поиска',
    )
    ai_provider = models.CharField(max_length=30, blank=True, verbose_name='AI-провайдер')
    ai_model = models.CharField(max_length=120, blank=True, verbose_name='AI-модель')
    queries = models.JSONField(default=list, blank=True, verbose_name='Запросы')
    coverage_before = models.JSONField(
        default=dict, blank=True, verbose_name='Полнота до исследования',
    )
    coverage_after = models.JSONField(
        default=dict, blank=True, verbose_name='Полнота после исследования',
    )
    result_count = models.PositiveIntegerField(default=0, verbose_name='Найдено страниц')
    claim_count = models.PositiveIntegerField(default=0, verbose_name='Найдено фактов')
    generate_after = models.BooleanField(
        default=False, verbose_name='Сгенерировать описание после завершения',
    )
    error_message = models.TextField(blank=True, verbose_name='Ошибка')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='Начато')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='Завершено')

    class Meta:
        verbose_name = 'Интернет-исследование товара'
        verbose_name_plural = 'Интернет-исследования товаров'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['product', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['product'],
                condition=models.Q(status__in=['queued', 'running']),
                name='unique_active_web_research_per_product',
            ),
        ]

    def __str__(self):
        return f'Web research #{self.pk} — product {self.product_id}'


class WebResearchEvidence(TimestampedModel):
    """Search result supplied to the model as a numbered evidence document."""

    run = models.ForeignKey(
        WebResearchRun, on_delete=models.CASCADE,
        related_name='evidence', verbose_name='Исследование',
    )
    query = models.CharField(max_length=500, verbose_name='Поисковый запрос')
    rank = models.PositiveSmallIntegerField(default=0, verbose_name='Позиция')
    title = models.CharField(max_length=500, blank=True, verbose_name='Заголовок')
    url = models.URLField(max_length=2000, verbose_name='URL')
    domain = models.CharField(max_length=255, db_index=True, verbose_name='Домен')
    snippet = models.TextField(blank=True, verbose_name='Фрагмент')

    class Meta:
        verbose_name = 'Доказательство интернет-исследования'
        verbose_name_plural = 'Доказательства интернет-исследований'
        ordering = ['rank', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'url'], name='unique_web_research_evidence_url',
            ),
        ]

    def __str__(self):
        return f'[{self.domain}] {self.title or self.url}'


class WebResearchClaim(TimestampedModel):
    """Structured model claim with explicit evidence links and saved-record pointer."""

    class ClaimType(models.TextChoices):
        BRAND = 'brand', 'Бренд'
        OEM = 'oem', 'OEM/Cross-код'
        FITMENT = 'fitment', 'Применяемость'
        FACT = 'fact', 'Факт/характеристика'

    run = models.ForeignKey(
        WebResearchRun, on_delete=models.CASCADE,
        related_name='claims', verbose_name='Исследование',
    )
    claim_type = models.CharField(
        max_length=30, choices=ClaimType.choices, verbose_name='Тип',
    )
    payload = models.JSONField(default=dict, verbose_name='Структурированные данные')
    confidence = models.FloatField(default=0.0, verbose_name='Уверенность')
    evidence = models.ManyToManyField(
        WebResearchEvidence, related_name='claims', verbose_name='Доказательства',
    )
    saved_model = models.CharField(max_length=80, blank=True, verbose_name='Модель результата')
    saved_record_id = models.PositiveBigIntegerField(
        null=True, blank=True, verbose_name='ID результата',
    )

    class Meta:
        verbose_name = 'Утверждение интернет-исследования'
        verbose_name_plural = 'Утверждения интернет-исследований'
        ordering = ['claim_type', 'id']

    def __str__(self):
        return f'{self.get_claim_type_display()} — run #{self.run_id}'
