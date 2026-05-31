from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.models import TimestampedModel
from apps.datasources.models import DataSourceConnection
from apps.tenants.models import Tenant


class TenantCatalogCategory(TimestampedModel):
    """Редактируемая категория каталога конкретного tenant-а."""

    class Domain(models.TextChoices):
        AUTO_PARTS = 'auto_parts', 'Автозапчасти'
        GENERIC = 'generic', 'Обычный товар'
        JEWELLERY = 'jewellery', 'Украшения'
        APPAREL = 'apparel', 'Одежда'
        UNKNOWN = 'unknown', 'Не определено'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='catalog_categories',
        verbose_name='Тенант',
    )
    name = models.CharField(max_length=200, verbose_name='Название')
    normalized_name = models.CharField(max_length=200, db_index=True, verbose_name='Нормализованное название')
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='children', verbose_name='Родительская категория',
    )
    domain = models.CharField(
        max_length=50, default=Domain.UNKNOWN,
        verbose_name='Домен',
    )
    aliases = ArrayField(
        models.CharField(max_length=150), default=list, blank=True,
        verbose_name='Алиасы',
    )
    external_source = models.CharField(max_length=50, blank=True, verbose_name='Источник')
    external_id = models.CharField(max_length=150, blank=True, verbose_name='Внешний ID')
    default_image_s3_key = models.CharField(
        max_length=500, blank=True, verbose_name='Картинка категории по умолчанию',
    )
    default_image_source_name = models.CharField(
        max_length=255, blank=True, verbose_name='Имя файла картинки по умолчанию',
    )
    is_active = models.BooleanField(default=True, verbose_name='Активна')

    class Meta:
        verbose_name = 'Категория каталога tenant-а'
        verbose_name_plural = 'Категории каталога tenant-а'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'normalized_name'],
                condition=models.Q(parent__isnull=True),
                name='unique_root_tenant_catalog_category_name',
            ),
            models.UniqueConstraint(
                fields=['tenant', 'parent', 'normalized_name'],
                condition=models.Q(parent__isnull=False),
                name='unique_child_tenant_catalog_category_name',
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.normalized_name = ''.join(char for char in self.name.lower() if char.isalnum())
        super().save(*args, **kwargs)


class TenantCategoryMapping(TimestampedModel):
    """Маппинг сырой категории источника на категорию каталога tenant-а."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='catalog_category_mappings',
        verbose_name='Тенант',
    )
    source_category = models.CharField(max_length=300, verbose_name='Категория источника')
    category = models.ForeignKey(
        TenantCatalogCategory, on_delete=models.CASCADE, related_name='source_mappings',
        verbose_name='Категория tenant-а',
    )

    class Meta:
        verbose_name = 'Маппинг категории каталога'
        verbose_name_plural = 'Маппинги категорий каталога'
        ordering = ['source_category']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'source_category'],
                name='unique_tenant_catalog_category_mapping',
            ),
        ]

    def __str__(self):
        return f'{self.source_category} -> {self.category}'


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
    catalog_category = models.ForeignKey(
        TenantCatalogCategory, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='products', verbose_name='Категория каталога',
    )
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
    description_ai = models.TextField(blank=True, verbose_name='AI-описание')
    title_ai = models.CharField(max_length=300, blank=True, verbose_name='AI-заголовок')

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


class ProductCatalogClassification(TimestampedModel):
    """Классификация домена товара для безопасного запуска domain-specific фич."""

    class Domain(models.TextChoices):
        AUTO_PARTS = 'auto_parts', 'Автозапчасть'
        GENERIC = 'generic', 'Обычный товар'
        JEWELLERY = 'jewellery', 'Украшение'
        APPAREL = 'apparel', 'Одежда'
        UNKNOWN = 'unknown', 'Не определено'

    class Source(models.TextChoices):
        RULES = 'rules', 'Правила'
        MANUAL = 'manual', 'Вручную'
        AI = 'ai', 'AI'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='product_catalog_classifications',
        verbose_name='Тенант',
    )
    product = models.OneToOneField(
        Product, on_delete=models.CASCADE, related_name='catalog_classification',
        verbose_name='Товар',
    )
    domain = models.CharField(
        max_length=50, default=Domain.UNKNOWN,
        verbose_name='Домен товара',
    )
    confidence = models.FloatField(default=0, verbose_name='Уверенность')
    source = models.CharField(
        max_length=30, choices=Source.choices, default=Source.RULES,
        verbose_name='Источник классификации',
    )
    reason = models.TextField(blank=True, verbose_name='Причина')
    needs_review = models.BooleanField(default=False, verbose_name='Нужна проверка')

    class Meta:
        verbose_name = 'Классификация товара'
        verbose_name_plural = 'Классификации товаров'
        indexes = [
            models.Index(fields=['tenant', 'domain']),
            models.Index(fields=['tenant', 'needs_review']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'product'],
                name='unique_product_catalog_classification',
            ),
        ]

    def __str__(self):
        return f'{self.product_id}: {self.domain} ({self.confidence:.2f})'


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


class ProductAttribute(TimestampedModel):
    """Структурированная характеристика товара из внешнего каталога."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='product_attributes',
        verbose_name='Тенант',
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='attributes',
        verbose_name='Товар',
    )
    source_id = models.CharField(max_length=50, default='tachka', verbose_name='Источник')
    name = models.CharField(max_length=150, verbose_name='Название')
    raw_name = models.CharField(max_length=150, blank=True, verbose_name='Исходное название')
    value = models.TextField(verbose_name='Значение')
    value_hash = models.CharField(max_length=64, blank=True, verbose_name='Хэш значения')

    class Meta:
        verbose_name = 'Характеристика товара'
        verbose_name_plural = 'Характеристики товаров'
        indexes = [
            models.Index(fields=['tenant', 'product']),
            models.Index(fields=['tenant', 'name']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'product', 'source_id', 'name', 'value_hash'],
                name='unique_product_attribute_value',
            ),
        ]

    def __str__(self):
        return f'{self.name}: {self.value}'


class ProductCrossCode(TimestampedModel):
    """OEM/cross-код с производителем и нормализованным кодом."""

    class CodeType(models.TextChoices):
        OEM = 'OEM', 'OEM'
        CROSS = 'Cross', 'Cross'
        TRADE = 'Trade', 'Trade'
        UNKNOWN = 'Unknown', 'Unknown'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='product_cross_codes',
        verbose_name='Тенант',
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='cross_codes',
        verbose_name='Товар',
    )
    source_id = models.CharField(max_length=50, default='tachka', verbose_name='Источник')
    manufacturer = models.CharField(max_length=100, blank=True, verbose_name='Производитель')
    code = models.CharField(max_length=100, verbose_name='Код')
    normalized_code = models.CharField(
        max_length=100, db_index=True, verbose_name='Нормализованный код',
    )
    code_type = models.CharField(
        max_length=20, choices=CodeType.choices, default=CodeType.UNKNOWN,
        verbose_name='Тип кода',
    )

    class Meta:
        verbose_name = 'OEM/Cross-код'
        verbose_name_plural = 'OEM/Cross-коды'
        indexes = [
            models.Index(fields=['tenant', 'normalized_code']),
            models.Index(fields=['tenant', 'manufacturer', 'normalized_code']),
            models.Index(fields=['product', 'code_type']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'tenant', 'product', 'source_id', 'manufacturer',
                    'normalized_code', 'code_type',
                ],
                name='unique_product_cross_code',
            ),
        ]

    def __str__(self):
        return f'{self.manufacturer} {self.code}'.strip()


class GlobalPart(TimestampedModel):
    """Platform-level справочная запчасть без tenant-коммерческих данных."""

    brand = models.CharField(max_length=100, blank=True, verbose_name='Бренд/производитель')
    normalized_brand = models.CharField(
        max_length=100, blank=True, db_index=True, verbose_name='Нормализованный бренд',
    )
    article = models.CharField(max_length=100, verbose_name='Артикул')
    normalized_article = models.CharField(
        max_length=100, db_index=True, verbose_name='Нормализованный артикул',
    )
    title = models.CharField(max_length=500, blank=True, verbose_name='Название из источника')
    source_id = models.CharField(max_length=50, blank=True, verbose_name='Первичный источник')
    source_url = models.URLField(blank=True, verbose_name='URL источника')
    confidence = models.FloatField(default=1.0, verbose_name='Уверенность')
    needs_review = models.BooleanField(default=False, verbose_name='Нужна проверка')
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено')

    class Meta:
        verbose_name = 'Глобальная запчасть'
        verbose_name_plural = 'Глобальные запчасти'
        indexes = [
            models.Index(fields=['normalized_article']),
            models.Index(fields=['normalized_brand', 'normalized_article']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['normalized_brand', 'normalized_article'],
                name='unique_global_part_identity',
            ),
        ]

    def __str__(self):
        return f'{self.brand} {self.article}'.strip()


class PartCategory(TimestampedModel):
    """Легкая platform taxonomy категорий автозапчастей."""

    name = models.CharField(max_length=150, verbose_name='Категория')
    normalized_name = models.CharField(
        max_length=150, unique=True, db_index=True, verbose_name='Нормализованная категория',
    )
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='children', verbose_name='Родительская категория',
    )
    aliases = ArrayField(
        models.CharField(max_length=150), default=list, blank=True,
        verbose_name='Алиасы',
    )
    fitment_required = models.BooleanField(
        default=True, verbose_name='Применяемость обязательна',
    )

    class Meta:
        verbose_name = 'Категория автозапчастей'
        verbose_name_plural = 'Категории автозапчастей'
        ordering = ['name']

    def __str__(self):
        return self.name


class GlobalPartRelation(TimestampedModel):
    """Platform-level связь между артикулами: OEM, аналог, заменитель, trade number."""

    class RelationType(models.TextChoices):
        OEM = 'OEM', 'OEM'
        CROSS = 'Cross', 'Cross'
        ANALOGUE = 'Analogue', 'Аналог'
        REPLACEMENT = 'Replacement', 'Замена'
        TRADE = 'Trade', 'Trade'
        UNKNOWN = 'Unknown', 'Unknown'

    source_part = models.ForeignKey(
        GlobalPart, on_delete=models.CASCADE, related_name='outgoing_relations',
        verbose_name='Исходная запчасть',
    )
    target_part = models.ForeignKey(
        GlobalPart, on_delete=models.CASCADE, related_name='incoming_relations',
        verbose_name='Связанная запчасть',
    )
    relation_type = models.CharField(
        max_length=30, choices=RelationType.choices, default=RelationType.UNKNOWN,
        verbose_name='Тип связи',
    )
    source_id = models.CharField(max_length=50, blank=True, verbose_name='Источник')
    source_url = models.URLField(blank=True, verbose_name='URL источника')
    raw_text = models.TextField(blank=True, verbose_name='Исходный текст')
    confidence = models.FloatField(default=1.0, verbose_name='Уверенность')
    needs_review = models.BooleanField(default=False, verbose_name='Нужна проверка')
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено')

    class Meta:
        verbose_name = 'Глобальная связь запчастей'
        verbose_name_plural = 'Глобальные связи запчастей'
        indexes = [
            models.Index(fields=['relation_type', 'source_id']),
            models.Index(fields=['source_part', 'relation_type']),
            models.Index(fields=['target_part', 'relation_type']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['source_part', 'target_part', 'relation_type', 'source_id'],
                name='unique_global_part_relation',
            ),
        ]

    def __str__(self):
        return f'{self.source_part} -> {self.target_part} [{self.relation_type}]'


class VehicleMake(TimestampedModel):
    """Нормализованная марка авто для platform-level справочника."""

    name = models.CharField(max_length=100, verbose_name='Марка')
    normalized_name = models.CharField(
        max_length=100, unique=True, db_index=True, verbose_name='Нормализованная марка',
    )
    aliases = ArrayField(
        models.CharField(max_length=100), default=list, blank=True,
        verbose_name='Алиасы',
    )

    class Meta:
        verbose_name = 'Марка авто'
        verbose_name_plural = 'Марки авто'
        ordering = ['name']

    def __str__(self):
        return self.name


class VehicleModel(TimestampedModel):
    """Нормализованная модель авто внутри марки."""

    make = models.ForeignKey(
        VehicleMake, on_delete=models.CASCADE, related_name='models',
        verbose_name='Марка',
    )
    name = models.CharField(max_length=150, verbose_name='Модель')
    normalized_name = models.CharField(
        max_length=150, db_index=True, verbose_name='Нормализованная модель',
    )
    aliases = ArrayField(
        models.CharField(max_length=150), default=list, blank=True,
        verbose_name='Алиасы',
    )

    class Meta:
        verbose_name = 'Модель авто'
        verbose_name_plural = 'Модели авто'
        ordering = ['make__name', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['make', 'normalized_name'],
                name='unique_vehicle_model_per_make',
            ),
        ]

    def __str__(self):
        return f'{self.make} {self.name}'.strip()


class VehicleFitment(TimestampedModel):
    """Применяемость товара к автомобилю."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='vehicle_fitments',
        verbose_name='Тенант',
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='fitments',
        verbose_name='Товар',
    )
    source_id = models.CharField(max_length=50, default='tachka', verbose_name='Источник')
    source_url = models.URLField(blank=True, verbose_name='URL источника')
    make = models.CharField(max_length=100, blank=True, verbose_name='Марка')
    model = models.CharField(max_length=150, verbose_name='Модель')
    generation = models.CharField(max_length=100, blank=True, verbose_name='Поколение')
    date_from = models.CharField(max_length=20, blank=True, verbose_name='Дата с')
    date_to = models.CharField(max_length=20, blank=True, verbose_name='Дата по')
    modification = models.CharField(max_length=255, blank=True, verbose_name='Модификация')
    engine_code = models.CharField(max_length=100, blank=True, verbose_name='Код двигателя/кузова')
    power_hp = models.PositiveIntegerField(null=True, blank=True, verbose_name='Мощность, л.с.')
    raw_text = models.TextField(blank=True, verbose_name='Исходная строка')
    confidence = models.FloatField(default=1.0, verbose_name='Уверенность')
    needs_review = models.BooleanField(default=False, verbose_name='Нужна проверка')
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено')

    class Meta:
        verbose_name = 'Применяемость'
        verbose_name_plural = 'Применяемость'
        indexes = [
            models.Index(fields=['tenant', 'make', 'model']),
            models.Index(fields=['tenant', 'product']),
            models.Index(fields=['product', 'needs_review']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'tenant', 'product', 'source_id', 'make', 'model', 'generation',
                    'modification', 'engine_code', 'power_hp',
                ],
                name='unique_vehicle_fitment',
            ),
        ]

    def __str__(self):
        return f'{self.make} {self.model} {self.generation}'.strip()


class GlobalPartFitment(TimestampedModel):
    """Platform-level применяемость артикула без привязки к tenant-товару."""

    part = models.ForeignKey(
        GlobalPart, on_delete=models.CASCADE, related_name='fitments',
        verbose_name='Глобальная запчасть',
    )
    vehicle_make = models.ForeignKey(
        VehicleMake, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='global_fitments', verbose_name='Нормализованная марка',
    )
    vehicle_model = models.ForeignKey(
        VehicleModel, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='global_fitments', verbose_name='Нормализованная модель',
    )
    make = models.CharField(max_length=100, blank=True, db_index=True, verbose_name='Марка')
    model = models.CharField(max_length=150, db_index=True, verbose_name='Модель')
    generation = models.CharField(max_length=100, blank=True, verbose_name='Поколение')
    date_from = models.CharField(max_length=20, blank=True, verbose_name='Дата с')
    date_to = models.CharField(max_length=20, blank=True, verbose_name='Дата по')
    modification = models.CharField(max_length=255, blank=True, verbose_name='Модификация')
    engine_code = models.CharField(max_length=100, blank=True, verbose_name='Код двигателя/кузова')
    power_hp = models.PositiveIntegerField(null=True, blank=True, verbose_name='Мощность, л.с.')
    source_id = models.CharField(max_length=50, blank=True, verbose_name='Источник')
    source_url = models.URLField(blank=True, verbose_name='URL источника')
    raw_text = models.TextField(blank=True, verbose_name='Исходная строка')
    confidence = models.FloatField(default=1.0, verbose_name='Уверенность')
    needs_review = models.BooleanField(default=False, verbose_name='Нужна проверка')
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено')

    class Meta:
        verbose_name = 'Глобальная применяемость'
        verbose_name_plural = 'Глобальная применяемость'
        indexes = [
            models.Index(fields=['make', 'model']),
            models.Index(fields=['vehicle_make', 'vehicle_model']),
            models.Index(fields=['part', 'needs_review']),
            models.Index(fields=['source_id', 'needs_review']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'part', 'source_id', 'make', 'model', 'generation',
                    'modification', 'engine_code', 'power_hp',
                ],
                name='unique_global_part_fitment',
            ),
        ]

    def __str__(self):
        return f'{self.part} -> {self.make} {self.model} {self.generation}'.strip()


class ProductEnrichmentFact(TimestampedModel):
    """Факт для достоверного AI-описания товара."""

    class FactType(models.TextChoices):
        TECHNICAL = 'technical', 'Технический'
        FITMENT = 'fitment', 'Применяемость'
        OEM = 'oem', 'OEM/Cross'
        DESCRIPTION_HINT = 'description_hint', 'Подсказка для описания'
        WARNING = 'warning', 'Предупреждение'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='product_enrichment_facts',
        verbose_name='Тенант',
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='enrichment_facts',
        verbose_name='Товар',
    )
    source_id = models.CharField(max_length=50, default='tachka', verbose_name='Источник')
    source_url = models.URLField(blank=True, verbose_name='URL источника')
    fact_type = models.CharField(
        max_length=30, choices=FactType.choices, verbose_name='Тип факта',
    )
    name = models.CharField(max_length=150, verbose_name='Название')
    value = models.TextField(verbose_name='Значение')
    value_hash = models.CharField(max_length=64, blank=True, verbose_name='Хэш значения')
    raw_text = models.TextField(blank=True, verbose_name='Исходный текст')
    confidence = models.FloatField(default=1.0, verbose_name='Уверенность')
    needs_review = models.BooleanField(default=False, verbose_name='Нужна проверка')
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено')

    class Meta:
        verbose_name = 'Факт обогащения товара'
        verbose_name_plural = 'Факты обогащения товаров'
        indexes = [
            models.Index(fields=['tenant', 'product']),
            models.Index(fields=['tenant', 'fact_type']),
            models.Index(fields=['product', 'needs_review']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'product', 'source_id', 'fact_type', 'name', 'value_hash'],
                name='unique_product_enrichment_fact',
            ),
        ]

    def __str__(self):
        return f'{self.fact_type}: {self.name}'


class ProductParseJob(TimestampedModel):
    """Попытка tenant-scoped обогащения товара из внешнего каталога."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает'
        RUNNING = 'running', 'В работе'
        SUCCESS = 'success', 'Успешно'
        FAILED = 'failed', 'Ошибка'
        NOT_FOUND = 'not_found', 'Не найдено'
        NEED_REVIEW = 'need_review', 'Нужна проверка'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='product_parse_jobs',
        verbose_name='Тенант',
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='parse_jobs', verbose_name='Товар',
    )
    brand = models.CharField(max_length=100, verbose_name='Бренд')
    article = models.CharField(max_length=100, verbose_name='Артикул')
    normalized_article = models.CharField(
        max_length=100, db_index=True, verbose_name='Нормализованный артикул',
    )
    source_id = models.CharField(max_length=50, default='tachka', verbose_name='Источник')
    source_url = models.URLField(blank=True, verbose_name='URL источника')
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.PENDING,
        verbose_name='Статус',
    )
    error_message = models.TextField(blank=True, verbose_name='Ошибка')
    raw_html = models.TextField(blank=True, verbose_name='Raw HTML')
    raw_text = models.TextField(blank=True, verbose_name='Raw text')
    parsed_data = models.JSONField(null=True, blank=True, verbose_name='Распарсенные данные')
    duration_ms = models.PositiveIntegerField(null=True, blank=True, verbose_name='Длительность, мс')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='Начато')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='Завершено')

    class Meta:
        verbose_name = 'Задача парсинга товара'
        verbose_name_plural = 'Задачи парсинга товаров'
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['source_id', 'normalized_article']),
            models.Index(fields=['tenant', 'source_id', 'normalized_article']),
        ]

    def __str__(self):
        return f'{self.source_id}: {self.brand} {self.article} [{self.status}]'


class ProductBulkActionJob(TimestampedModel):
    """Throttled массовое действие по товарам tenant-а."""

    class Action(models.TextChoices):
        ENRICH_SELECTED = 'enrich_selected', 'Обогатить выбранные'
        ENRICH_MISSING_DATA = 'enrich_missing_data', 'Обогатить отсутствующие данные'
        GENERATE_DESCRIPTIONS = 'generate_descriptions', 'Сгенерировать описания'
        ENRICH_THEN_GENERATE = (
            'enrich_then_generate_description',
            'Обогатить и сгенерировать описания',
        )
        CLASSIFY_CATALOG_DOMAIN = 'classify_catalog_domain', 'Определить домен товаров'
        FIND_IMAGES = 'find_images', 'Найти изображения'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает'
        RUNNING = 'running', 'В работе'
        PAUSED = 'paused', 'На паузе'
        COOLING_DOWN = 'cooling_down', 'Охлаждение'
        SUCCESS = 'success', 'Успешно'
        FAILED = 'failed', 'Ошибка'
        CANCELLED = 'cancelled', 'Отменено'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='product_bulk_action_jobs',
        verbose_name='Тенант',
    )
    action = models.CharField(max_length=50, choices=Action.choices, verbose_name='Действие')
    source_id = models.CharField(max_length=50, default='tachka', verbose_name='Источник')
    product_ids = models.JSONField(default=list, verbose_name='ID товаров')
    filters = models.JSONField(default=dict, blank=True, verbose_name='Фильтры')
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.PENDING,
        verbose_name='Статус',
    )
    total_count = models.PositiveIntegerField(default=0, verbose_name='Всего')
    queued_count = models.PositiveIntegerField(default=0, verbose_name='Поставлено в очередь')
    processed_count = models.PositiveIntegerField(default=0, verbose_name='Обработано')
    success_count = models.PositiveIntegerField(default=0, verbose_name='Успешно')
    skipped_count = models.PositiveIntegerField(default=0, verbose_name='Пропущено')
    failed_count = models.PositiveIntegerField(default=0, verbose_name='Ошибки')
    batch_size = models.PositiveIntegerField(default=20, verbose_name='Размер batch')
    pause_seconds = models.PositiveIntegerField(default=60, verbose_name='Пауза между batch, сек')
    next_batch_at = models.DateTimeField(null=True, blank=True, verbose_name='Следующий batch')
    error_message = models.TextField(blank=True, verbose_name='Ошибка')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='Начато')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='Завершено')

    class Meta:
        verbose_name = 'Массовое действие по товарам'
        verbose_name_plural = 'Массовые действия по товарам'
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['tenant', 'status']),
        ]

    def __str__(self):
        return f'{self.action} [{self.status}] — {self.total_count}'
