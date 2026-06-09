from django.db import models
from django.utils import timezone

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class ImageSearchLog(TimestampedModel):
    """Лог каждого запроса к источнику изображений.

    Хранит информацию о запросе, результатах и ошибках.
    Используется для мониторинга hit rate и здоровья источников.
    """

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
    error = models.TextField(blank=True, verbose_name='Ошибка')

    class Meta:
        verbose_name = 'Лог поиска изображений'
        verbose_name_plural = 'Логи поиска изображений'
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['product', 'created_at']),
            models.Index(fields=['source_id', 'created_at']),
        ]

    def __str__(self):
        return f'[{self.source_id}] {self.product_id} — {self.results_count} results'


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
