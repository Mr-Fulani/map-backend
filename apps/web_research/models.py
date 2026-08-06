from django.db import models
from django.utils.timezone import now

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class TenantWebResearchSettings(TimestampedModel):
    """Tenant-owned market research policy; provider credentials stay platform-owned."""

    class RegionPreset(models.TextChoices):
        RUSSIA = 'russia', 'Россия'
        RUSSIA_CIS = 'russia_cis', 'Россия и СНГ'
        CUSTOM = 'custom', 'Выбранные страны'
        WORLDWIDE = 'worldwide', 'Без ограничения'

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE,
        related_name='web_research_settings', verbose_name='Тенант',
    )
    market_research_enabled = models.BooleanField(default=True, verbose_name='Мониторинг цен')
    region_preset = models.CharField(
        max_length=20, choices=RegionPreset.choices,
        default=RegionPreset.RUSSIA, verbose_name='Регион поиска',
    )
    country_codes = models.JSONField(default=list, blank=True, verbose_name='Коды стран')
    search_language = models.CharField(max_length=10, default='ru', verbose_name='Язык поиска')
    include_marketplaces = models.BooleanField(default=True, verbose_name='Маркетплейсы')
    include_used = models.BooleanField(default=False, verbose_name='Товары б/у')
    include_preorder = models.BooleanField(default=True, verbose_name='Под заказ')
    include_analogues = models.BooleanField(default=False, verbose_name='Аналоги')
    exact_matches_only = models.BooleanField(default=True, verbose_name='Только точные совпадения')
    preferred_domains = models.JSONField(default=list, blank=True, verbose_name='Приоритетные домены')
    excluded_domains = models.JSONField(default=list, blank=True, verbose_name='Исключённые домены')
    result_limit = models.PositiveSmallIntegerField(default=30, verbose_name='Лимит результатов')
    price_ttl_hours = models.PositiveSmallIntegerField(default=24, verbose_name='Срок цены, часов')
    display_currency = models.CharField(max_length=3, default='RUB', verbose_name='Валюта')

    class Meta:
        verbose_name = 'Настройки интернет-исследования тенанта'
        verbose_name_plural = 'Настройки интернет-исследования тенантов'

    def __str__(self):
        return f'{self.tenant}: {self.get_region_preset_display()}'

    def snapshot(self) -> dict:
        return {
            'region_preset': self.region_preset,
            'country_codes': list(self.country_codes or []),
            'search_language': self.search_language,
            'include_marketplaces': self.include_marketplaces,
            'include_used': self.include_used,
            'include_preorder': self.include_preorder,
            'include_analogues': self.include_analogues,
            'exact_matches_only': self.exact_matches_only,
            'preferred_domains': list(self.preferred_domains or []),
            'excluded_domains': list(self.excluded_domains or []),
            'result_limit': self.result_limit,
            'price_ttl_hours': self.price_ttl_hours,
            'display_currency': self.display_currency,
        }


class WebResearchRun(TimestampedModel):
    """Auditable product-research run, independent from concrete providers."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'В очереди'
        RUNNING = 'running', 'Выполняется'
        NEED_REVIEW = 'need_review', 'Нужна проверка'
        COMPLETED = 'completed', 'Проверено'
        NO_RESULTS = 'no_results', 'Ничего не найдено'
        SKIPPED = 'skipped', 'Не требуется'
        FAILED = 'failed', 'Ошибка'

    class Trigger(models.TextChoices):
        MANUAL = 'manual', 'Вручную'
        PARSER_FALLBACK = 'parser_fallback', 'Fallback после каталогов'

    class Purpose(models.TextChoices):
        ENRICHMENT = 'enrichment', 'Техническое обогащение'
        PRICING = 'pricing', 'Мониторинг цен'
        COMBINED = 'combined', 'Обогащение и цены'

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
    purpose = models.CharField(
        max_length=20, choices=Purpose.choices, default=Purpose.ENRICHMENT,
        db_index=True, verbose_name='Назначение',
    )
    settings_snapshot = models.JSONField(
        default=dict, blank=True, verbose_name='Снимок настроек поиска',
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
    offer_count = models.PositiveIntegerField(default=0, verbose_name='Найдено предложений')
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
                condition=(
                    models.Q(status__in=['queued', 'running'])
                    & models.Q(purpose='enrichment')
                ),
                name='unique_active_enrichment_research_per_product',
            ),
            models.UniqueConstraint(
                fields=['product'],
                condition=(
                    models.Q(status__in=['queued', 'running'])
                    & models.Q(purpose__in=['pricing', 'combined'])
                ),
                name='unique_active_pricing_research_per_product',
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
    provider_id = models.SlugField(
        max_length=50, blank=True, verbose_name='Провайдер поиска',
    )
    title = models.CharField(max_length=500, blank=True, verbose_name='Заголовок')
    url = models.URLField(max_length=2000, verbose_name='URL')
    domain = models.CharField(max_length=255, db_index=True, verbose_name='Домен')
    snippet = models.TextField(blank=True, verbose_name='Фрагмент')
    raw_content = models.TextField(blank=True, verbose_name='Структурированное содержимое')

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


class CompetitorOffer(TimestampedModel):
    """Immutable-ish product-level market observation grounded in saved evidence."""

    class MatchType(models.TextChoices):
        EXACT = 'exact', 'Точное совпадение'
        CROSS = 'cross', 'Совпадение по OEM/Cross-коду'
        ANALOGUE = 'analogue', 'Возможный аналог'
        REVIEW = 'review', 'Требуется проверка'

    class Availability(models.TextChoices):
        UNKNOWN = 'unknown', 'Наличие не указано'
        IN_STOCK = 'in_stock', 'В наличии'
        PREORDER = 'preorder', 'Под заказ'
        OUT_OF_STOCK = 'out_of_stock', 'Нет в наличии'

    class Condition(models.TextChoices):
        NEW = 'new', 'Новый'
        USED = 'used', 'Б/у'
        UNKNOWN = 'unknown', 'Не указано'

    class ReviewStatus(models.TextChoices):
        PENDING = 'pending', 'Ожидает проверки'
        VERIFIED = 'verified', 'Подтверждено'
        REJECTED = 'rejected', 'Отклонено'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='competitor_offers', verbose_name='Тенант',
    )
    product = models.ForeignKey(
        'products.Product', on_delete=models.CASCADE,
        related_name='competitor_offers', verbose_name='Товар',
    )
    run = models.ForeignKey(
        WebResearchRun, on_delete=models.PROTECT,
        related_name='offers', verbose_name='Исследование',
    )
    evidence = models.ForeignKey(
        WebResearchEvidence, on_delete=models.PROTECT,
        related_name='offers', verbose_name='Доказательство',
    )
    provider_id = models.SlugField(max_length=50, blank=True, verbose_name='Провайдер')
    seller_name = models.CharField(max_length=255, blank=True, verbose_name='Продавец')
    domain = models.CharField(max_length=255, db_index=True, verbose_name='Домен')
    url = models.URLField(max_length=2000, verbose_name='URL')
    dedupe_key = models.CharField(max_length=64, db_index=True, verbose_name='Ключ дедупликации')
    country_code = models.CharField(max_length=2, blank=True, db_index=True, verbose_name='Страна')
    region = models.CharField(max_length=150, blank=True, verbose_name='Регион')
    title = models.CharField(max_length=500, blank=True, verbose_name='Название')
    article = models.CharField(max_length=100, blank=True, verbose_name='Артикул')
    matched_code = models.CharField(max_length=100, blank=True, verbose_name='Совпавший код')
    match_type = models.CharField(
        max_length=20, choices=MatchType.choices,
        default=MatchType.REVIEW, db_index=True, verbose_name='Тип совпадения',
    )
    match_confidence = models.FloatField(default=0.0, verbose_name='Точность совпадения')
    match_reasons = models.JSONField(default=list, blank=True, verbose_name='Причины совпадения')
    price = models.DecimalField(max_digits=14, decimal_places=2, verbose_name='Исходная цена')
    currency = models.CharField(max_length=3, verbose_name='Исходная валюта')
    normalized_price = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        verbose_name='Нормализованная цена',
    )
    normalized_currency = models.CharField(max_length=3, default='RUB', verbose_name='Валюта сравнения')
    is_price_from = models.BooleanField(default=False, verbose_name='Цена от')
    availability = models.CharField(
        max_length=20, choices=Availability.choices,
        default=Availability.UNKNOWN, db_index=True, verbose_name='Наличие',
    )
    availability_text = models.CharField(max_length=255, blank=True, verbose_name='Текст наличия')
    quantity = models.PositiveIntegerField(null=True, blank=True, verbose_name='Количество')
    condition = models.CharField(
        max_length=20, choices=Condition.choices,
        default=Condition.UNKNOWN, db_index=True, verbose_name='Состояние',
    )
    delivery_text = models.CharField(max_length=500, blank=True, verbose_name='Доставка')
    review_status = models.CharField(
        max_length=20, choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING, db_index=True, verbose_name='Проверка',
    )
    captured_at = models.DateTimeField(default=now, db_index=True, verbose_name='Проверено')
    expires_at = models.DateTimeField(db_index=True, verbose_name='Актуально до')

    class Meta:
        verbose_name = 'Рыночное предложение'
        verbose_name_plural = 'Рыночные предложения'
        ordering = ['normalized_price', '-captured_at']
        indexes = [
            models.Index(fields=['tenant', 'product', '-captured_at']),
            models.Index(fields=['tenant', 'product', 'review_status', 'expires_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'dedupe_key'], name='unique_competitor_offer_per_run',
            ),
        ]

    def __str__(self):
        return f'{self.seller_name or self.domain}: {self.price} {self.currency}'


class WebSearchConnection(TimestampedModel):
    """Platform-owned, encrypted connection to a registered search adapter."""

    PRIMARY_PRIORITY = 10
    FALLBACK_PRIORITY = 20

    class CheckStatus(models.TextChoices):
        NOT_CHECKED = 'not_checked', 'Не проверено'
        OK = 'ok', 'Подключено'
        ERROR = 'error', 'Ошибка'

    provider_id = models.SlugField(
        max_length=50, unique=True, verbose_name='Провайдер',
    )
    display_name = models.CharField(max_length=120, verbose_name='Название')
    is_active = models.BooleanField(default=True, verbose_name='Активно')
    priority = models.PositiveSmallIntegerField(default=100, verbose_name='Приоритет')
    allowed_plan_slugs = models.JSONField(
        default=list, blank=True, verbose_name='Доступно тарифам',
        help_text='Пустой список — доступно всем тарифам.',
    )
    credentials_enc = models.BinaryField(null=True, blank=True, editable=False)
    parameters = models.JSONField(
        default=dict, blank=True, verbose_name='Параметры',
        help_text='Безопасные настройки запроса без секретов.',
    )
    requests_per_minute = models.PositiveIntegerField(
        default=60, verbose_name='Запросов в минуту',
    )
    monthly_request_limit = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Месячный лимит запросов',
    )
    last_check_status = models.CharField(
        max_length=20, choices=CheckStatus.choices,
        default=CheckStatus.NOT_CHECKED, verbose_name='Состояние подключения',
    )
    last_check_message = models.CharField(
        max_length=500, blank=True, verbose_name='Результат проверки',
    )
    last_checked_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Последняя проверка',
    )

    class Meta:
        verbose_name = 'Подключение интернет-поиска'
        verbose_name_plural = 'Подключения интернет-поиска'
        ordering = ['priority', 'display_name']

    def __str__(self):
        return self.display_name

    def set_credentials(self, credentials: dict) -> None:
        from apps.datasources.encryption import encrypt
        self.credentials_enc = encrypt(credentials) if credentials else None

    def get_credentials(self) -> dict:
        if not self.credentials_enc:
            return {}
        from apps.datasources.encryption import decrypt
        return decrypt(bytes(self.credentials_enc))

    @property
    def has_credentials(self) -> bool:
        return bool(self.credentials_enc)

    def mark_checked(self, *, ok: bool, message: str = '') -> None:
        self.last_check_status = self.CheckStatus.OK if ok else self.CheckStatus.ERROR
        self.last_check_message = message[:500]
        self.last_checked_at = now()
        self.save(update_fields=[
            'last_check_status', 'last_check_message', 'last_checked_at', 'updated_at',
        ])


class WebSearchAttempt(TimestampedModel):
    """One provider request within a research run, including fallback failures."""

    class Status(models.TextChoices):
        SUCCESS = 'success', 'Успешно'
        EMPTY = 'empty', 'Нет результатов'
        FAILED = 'failed', 'Ошибка'
        SKIPPED = 'skipped', 'Пропущено'

    run = models.ForeignKey(
        WebResearchRun, on_delete=models.CASCADE,
        related_name='search_attempts', verbose_name='Исследование',
    )
    connection = models.ForeignKey(
        WebSearchConnection, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='attempts', verbose_name='Подключение',
    )
    provider_id = models.SlugField(max_length=50, verbose_name='Провайдер')
    query = models.CharField(max_length=500, verbose_name='Запрос')
    status = models.CharField(max_length=20, choices=Status.choices, verbose_name='Статус')
    result_count = models.PositiveSmallIntegerField(default=0, verbose_name='Результатов')
    duration_ms = models.PositiveIntegerField(default=0, verbose_name='Длительность, мс')
    retryable = models.BooleanField(default=False, verbose_name='Можно повторить')
    error_code = models.CharField(max_length=80, blank=True, verbose_name='Код ошибки')
    error_message = models.CharField(max_length=500, blank=True, verbose_name='Ошибка')

    class Meta:
        verbose_name = 'Попытка интернет-поиска'
        verbose_name_plural = 'Попытки интернет-поиска'
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(
                fields=['provider_id', '-created_at'],
                name='websearch_provider_recent_idx',
            ),
            models.Index(
                fields=['run', 'created_at'], name='websearch_run_created_idx',
            ),
        ]

    def __str__(self):
        return f'{self.provider_id}: {self.get_status_display()}'


class WebResearchClaim(TimestampedModel):
    """Structured model claim with explicit evidence links and saved-record pointer."""

    class ClaimType(models.TextChoices):
        BRAND = 'brand', 'Бренд'
        OEM = 'oem', 'OEM/Cross-код'
        FITMENT = 'fitment', 'Применяемость'
        FACT = 'fact', 'Факт/характеристика'

    class ReviewStatus(models.TextChoices):
        PENDING = 'pending', 'Ожидает проверки'
        APPROVED = 'approved', 'Одобрено'
        REJECTED = 'rejected', 'Отклонено'

    run = models.ForeignKey(
        WebResearchRun, on_delete=models.CASCADE,
        related_name='claims', verbose_name='Исследование',
    )
    claim_type = models.CharField(
        max_length=30, choices=ClaimType.choices, verbose_name='Тип',
    )
    payload = models.JSONField(default=dict, verbose_name='Структурированные данные')
    confidence = models.FloatField(default=0.0, verbose_name='Уверенность')
    review_status = models.CharField(
        max_length=20, choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING, verbose_name='Проверка',
    )
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
