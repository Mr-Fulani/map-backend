from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.models import TimestampedModel
from apps.datasources.models import DataSourceConnection
from apps.tenants.models import Tenant


class Product(TimestampedModel):
    """Товар из источника данных (1С, CSV и т.д.)."""

    CONDITION_NEW = 'new'
    CONDITION_USED = 'used'
    CONDITION_CHOICES = [(CONDITION_NEW, 'Новый'), (CONDITION_USED, 'Б/у')]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='products', verbose_name='Тенант',
    )
    datasource = models.ForeignKey(
        DataSourceConnection, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='products', verbose_name='Источник данных',
    )
    uuid_1c = models.UUIDField(null=True, blank=True, verbose_name='UUID из 1С')
    article = models.CharField(max_length=100, verbose_name='Артикул')
    cross_numbers = ArrayField(
        models.CharField(max_length=50), default=list, blank=True, verbose_name='Кросс-номера',
    )
    oem_numbers = ArrayField(
        models.CharField(max_length=50), default=list, blank=True, verbose_name='OEM-номера',
    )
    name = models.CharField(max_length=500, verbose_name='Наименование')
    brand = models.CharField(max_length=200, blank=True, verbose_name='Бренд')
    category_1c = models.CharField(max_length=300, blank=True, verbose_name='Категория из 1С')
    condition = models.CharField(
        max_length=10, choices=CONDITION_CHOICES, default=CONDITION_NEW, verbose_name='Состояние',
    )
    applicability = models.JSONField(default=list, verbose_name='Применимость')
    description_1c = models.TextField(blank=True, verbose_name='Описание из 1С')
    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='Цена, ₽')
    stock_qty = models.PositiveIntegerField(default=0, verbose_name='Остаток на складе')
    warehouse = models.CharField(max_length=200, blank=True, verbose_name='Склад')
    export_enabled = models.BooleanField(default=False, verbose_name='Экспорт на Avito')
    sync_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата синхронизации')
    hash_1c = models.CharField(max_length=64, blank=True, verbose_name='Хэш данных из 1С')

    class Meta:
        verbose_name = 'Товар'
        verbose_name_plural = 'Товары'
        indexes = [
            models.Index(fields=['tenant', 'article']),
            models.Index(fields=['tenant', 'export_enabled', 'sync_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'uuid_1c'],
                condition=models.Q(uuid_1c__isnull=False),
                name='unique_tenant_uuid_1c',
            ),
            models.UniqueConstraint(
                fields=['tenant', 'datasource', 'article'],
                name='unique_tenant_datasource_article',
            ),
        ]

    def __str__(self):
        return f'{self.article} — {self.name}'


class ProductImage(models.Model):
    """Изображение товара, хранится в S3."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='images', verbose_name='Товар',
    )
    s3_key = models.CharField(max_length=500, verbose_name='Ключ S3')
    s3_key_thumb = models.CharField(max_length=500, blank=True, verbose_name='Ключ S3 (миниатюра)')
    url_source = models.URLField(blank=True, verbose_name='Исходный URL изображения')
    sha256 = models.CharField(max_length=64, blank=True, verbose_name='SHA256')
    position = models.PositiveSmallIntegerField(default=0, verbose_name='Позиция')
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name='Загружено')

    class Meta:
        verbose_name = 'Изображение товара'
        verbose_name_plural = 'Изображения товаров'
        ordering = ['position']
        unique_together = [('product', 'sha256')]
