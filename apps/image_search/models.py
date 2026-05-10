from django.db import models

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
