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
    image_status = models.CharField(
        max_length=20, blank=True, default='', verbose_name='Статус изображений',
        choices=[
            ('', 'Не проверено'),
            ('has_images', 'Есть фото'),
            ('no_image', 'Нет фото'),
            ('searching', 'Идёт поиск'),
        ],
    )

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
    """Изображение товара, хранится в S3.

    Поддерживает два сценария:
    - Фото из источника данных (1С/CSV) — status='imported', source_id='1c'/'csv'
    - Автоматически найденное — status='auto_approved'/'needs_review'/etc.
    """

    class Status(models.TextChoices):
        IMPORTED = 'imported', 'Из источника'
        AUTO_APPROVED = 'auto_approved', 'Авто-одобрено'
        NEEDS_REVIEW = 'needs_review', 'На проверке'
        LOW_CONFIDENCE = 'low_confidence', 'Низкая уверенность'
        MANUALLY_SET = 'manually_set', 'Загружено вручную'
        REJECTED = 'rejected', 'Отклонено'

    class SearchConfidence(models.TextChoices):
        HIGH = 'high', 'Высокая (по артикулу)'
        MEDIUM = 'medium', 'Средняя'
        LOW = 'low', 'Низкая (по описанию)'
        VERY_LOW = 'very_low', 'Очень низкая (нет артикула)'

    # === Существующие поля (не менять!) ===
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='images', verbose_name='Товар',
    )
    s3_key = models.CharField(max_length=500, verbose_name='Ключ S3')
    s3_key_thumb = models.CharField(max_length=500, blank=True, verbose_name='Ключ S3 (миниатюра)')
    url_source = models.URLField(blank=True, verbose_name='Исходный URL изображения')
    sha256 = models.CharField(max_length=64, blank=True, verbose_name='SHA256')
    position = models.PositiveSmallIntegerField(default=0, verbose_name='Позиция')
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name='Загружено')

    # === Новые поля (image_search, все nullable/defaults) ===
    s3_key_preview = models.CharField(
        max_length=500, blank=True, verbose_name='Ключ S3 (превью 600px)',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.IMPORTED,
        verbose_name='Статус',
    )
    source_id = models.CharField(
        max_length=50, blank=True, verbose_name='Источник',
        help_text='autodoc, exist, duckduckgo, 1c, csv, manual',
    )
    tier = models.PositiveSmallIntegerField(
        null=True, blank=True, verbose_name='Приоритет источника',
    )
    quality_score = models.FloatField(
        null=True, blank=True, verbose_name='Оценка качества (0–1)',
    )
    search_confidence = models.CharField(
        max_length=10, choices=SearchConfidence.choices, blank=True,
        verbose_name='Уверенность поиска',
    )
    phash = models.CharField(
        max_length=64, blank=True, db_index=True, verbose_name='Perceptual hash',
    )
    resolution_w = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Ширина (px)',
    )
    resolution_h = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Высота (px)',
    )
    file_size_kb = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Размер файла (KB)',
    )
    is_primary = models.BooleanField(default=False, verbose_name='Главное фото')
    seo_filename = models.CharField(
        max_length=255, blank=True, verbose_name='SEO-имя файла',
    )
    reviewed_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Дата проверки',
    )
    reviewed_by = models.ForeignKey(
        'users.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='reviewed_images', verbose_name='Проверил',
    )

    class Meta:
        verbose_name = 'Изображение товара'
        verbose_name_plural = 'Изображения товаров'
        ordering = ['position']
        unique_together = [('product', 'sha256')]
        indexes = [
            models.Index(fields=['status', 'quality_score']),
            models.Index(fields=['product', 'is_primary']),
        ]

    def __str__(self):
        return f'Image #{self.pk} [{self.status}] — {self.product_id}'
