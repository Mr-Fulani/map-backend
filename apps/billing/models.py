import re
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from django.db import models
from django.utils import timezone

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class Plan(TimestampedModel):
    """Тарифный план платформы."""

    SLUG_STARTER = 'starter'
    SLUG_BUSINESS = 'business'
    SLUG_PRO = 'pro'
    SLUG_ENTERPRISE = 'enterprise'

    name = models.CharField(max_length=50, verbose_name='Название')
    slug = models.SlugField(unique=True, verbose_name='Slug')
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Цена в месяц')
    price_yearly = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Цена в год')

    # Для листингов/SKU null означает отсутствие лимита.
    # Для AI null безопасно трактуется сервисом как 0, а не как безлимит.
    limit_listings = models.PositiveIntegerField(null=True, blank=True, verbose_name='Лимит листингов')
    limit_sku = models.PositiveIntegerField(null=True, blank=True, verbose_name='Лимит SKU')
    limit_ai_credits = models.PositiveIntegerField(null=True, blank=True, verbose_name='Лимит AI-кредитов')

    is_active = models.BooleanField(default=True, verbose_name='Активен')

    class Meta:
        verbose_name = 'Тарифный план'
        verbose_name_plural = 'Тарифные планы'
        ordering = ['price_monthly']

    def __str__(self):
        return self.name

    @property
    def price_yearly_monthly_equivalent(self):
        return (self.price_yearly / Decimal('12')).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP,
        )


class Subscription(TimestampedModel):
    """Подписка тенанта на тарифный план."""

    STATUS_TRIAL = 'trial'
    STATUS_ACTIVE = 'active'
    STATUS_PAST_DUE = 'past_due'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = [
        (STATUS_TRIAL, 'Пробный период'),
        (STATUS_ACTIVE, 'Активна'),
        (STATUS_PAST_DUE, 'Просрочена'),
        (STATUS_CANCELLED, 'Отменена'),
    ]

    ACCESS_FULL = 'full'
    ACCESS_BILLING_ONLY = 'billing_only'

    PERIOD_MONTHLY = 'monthly'
    PERIOD_YEARLY = 'yearly'

    PERIOD_CHOICES = [
        (PERIOD_MONTHLY, 'Ежемесячно'),
        (PERIOD_YEARLY, 'Ежегодно'),
    ]

    tenant = models.OneToOneField(
        Tenant,
        on_delete=models.CASCADE,
        related_name='subscription',
        verbose_name='Тенант',
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, verbose_name='Тарифный план')
    status = models.CharField(
        choices=STATUS_CHOICES, default=STATUS_TRIAL, max_length=20, verbose_name='Статус',
    )
    billing_period = models.CharField(
        choices=PERIOD_CHOICES, default=PERIOD_MONTHLY, max_length=10, verbose_name='Период оплаты',
    )
    current_period_start = models.DateField(verbose_name='Начало периода')
    current_period_end = models.DateField(verbose_name='Конец периода')
    ai_period_start = models.DateField(
        null=True,
        blank=True,
        verbose_name='Начало периода AI-кредитов',
    )
    ai_period_end = models.DateField(
        null=True,
        blank=True,
        verbose_name='Конец периода AI-кредитов',
    )
    yookassa_subscription_id = models.CharField(
        max_length=200, blank=True, verbose_name='ID подписки ЮKassa',
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата отмены')
    billing_version = models.PositiveBigIntegerField(
        default=0,
        verbose_name='Версия биллингового состояния',
    )

    class Meta:
        verbose_name = 'Подписка'
        verbose_name_plural = 'Подписки'

    def __str__(self):
        return f'{self.tenant.slug} — {self.plan.name} ({self.status})'

    @property
    def effective_status(self):
        """Статус с учётом даты, даже если фоновая задача ещё не отработала."""
        if (
            self.status in (self.STATUS_TRIAL, self.STATUS_ACTIVE)
            and self.current_period_end < timezone.localdate()
        ):
            return self.STATUS_PAST_DUE
        return self.status

    @property
    def is_active(self):
        return self.effective_status in (self.STATUS_TRIAL, self.STATUS_ACTIVE)

    @property
    def access_mode(self):
        """Полный доступ либо read-only с доступом к восстановлению оплаты."""
        return self.ACCESS_FULL if self.is_active else self.ACCESS_BILLING_ONLY


class Invoice(TimestampedModel):
    """Счёт на оплату."""

    STATUS_PENDING = 'pending'
    STATUS_PAID = 'paid'
    STATUS_FAILED = 'failed'
    STATUS_PARTIALLY_REFUNDED = 'partially_refunded'
    STATUS_REFUNDED = 'refunded'
    STATUS_MANUAL_REVIEW = 'manual_review'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Ожидает оплаты'),
        (STATUS_PAID, 'Оплачен'),
        (STATUS_FAILED, 'Ошибка оплаты'),
        (STATUS_PARTIALLY_REFUNDED, 'Частично возвращён'),
        (STATUS_REFUNDED, 'Возвращён'),
        (STATUS_MANUAL_REVIEW, 'Требует ручной проверки'),
    ]

    TYPE_SUBSCRIPTION = 'subscription'
    TYPE_AI_TOPUP = 'ai_topup'
    TYPE_CHOICES = [
        (TYPE_SUBSCRIPTION, 'Подписка'),
        (TYPE_AI_TOPUP, 'Пополнение AI-баланса'),
    ]

    CHECKOUT_LEGACY = 'legacy'
    CHECKOUT_INTENT_CREATED = 'intent_created'
    CHECKOUT_PROVIDER_PENDING = 'provider_pending'
    CHECKOUT_PROVIDER_CREATED = 'provider_created'
    CHECKOUT_MANUAL_REVIEW = 'manual_review'
    CHECKOUT_STATE_CHOICES = [
        (CHECKOUT_LEGACY, 'Legacy счёт'),
        (CHECKOUT_INTENT_CREATED, 'Намерение создано'),
        (CHECKOUT_PROVIDER_PENDING, 'Результат провайдера неизвестен'),
        (CHECKOUT_PROVIDER_CREATED, 'Платёж у провайдера создан'),
        (CHECKOUT_MANUAL_REVIEW, 'Требует ручной проверки'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='invoices', verbose_name='Тенант',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Сумма, ₽')
    currency = models.CharField(max_length=3, default='RUB', verbose_name='Валюта')
    purchase_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES, default=TYPE_SUBSCRIPTION,
        verbose_name='Тип покупки',
    )
    metadata = models.JSONField(default=dict, blank=True, verbose_name='Метаданные')
    status = models.CharField(
        choices=STATUS_CHOICES, default=STATUS_PENDING, max_length=20, verbose_name='Статус',
    )
    yookassa_payment_id = models.CharField(max_length=200, blank=True, verbose_name='ID платежа ЮKassa')
    pdf_s3_key = models.CharField(max_length=500, blank=True, verbose_name='Ключ PDF в S3')
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата оплаты')
    refunded_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0'),
        verbose_name='Возвращено',
    )
    refund_review_required = models.BooleanField(
        default=False,
        verbose_name='Возврат требует ручной проверки',
    )
    checkout_client_key = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Ключ идемпотентности клиента',
    )
    provider_idempotency_key = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name='Ключ идемпотентности YooKassa',
    )
    checkout_payload_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name='Хеш checkout payload',
    )
    checkout_return_url = models.URLField(
        max_length=2048,
        blank=True,
        editable=False,
        verbose_name='Return URL checkout',
    )
    checkout_confirmation_url = models.URLField(
        max_length=2048,
        blank=True,
        editable=False,
        verbose_name='Confirmation URL YooKassa',
    )
    checkout_state = models.CharField(
        max_length=24,
        choices=CHECKOUT_STATE_CHOICES,
        default=CHECKOUT_LEGACY,
        verbose_name='Состояние checkout intent',
    )
    checkout_attempt_count = models.PositiveSmallIntegerField(default=0)
    checkout_first_attempt_at = models.DateTimeField(null=True, blank=True)
    checkout_last_attempt_at = models.DateTimeField(null=True, blank=True)
    checkout_last_error = models.CharField(max_length=500, blank=True)
    entitlement_snapshot = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        verbose_name='Неизменяемый снимок покупки',
    )
    entitlement_plan = models.ForeignKey(
        Plan,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name='purchase_invoices',
        verbose_name='Купленный тариф',
    )
    expected_subscription_version = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
        verbose_name='Ожидаемая версия подписки',
    )
    reconciliation_attempts = models.PositiveSmallIntegerField(default=0)
    next_reconciliation_at = models.DateTimeField(null=True, blank=True)
    last_reconciliation_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Счёт'
        verbose_name_plural = 'Счета'
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(
                fields=['checkout_state', 'next_reconciliation_at'],
                name='billing_inv_reconcile_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['yookassa_payment_id'],
                condition=~models.Q(yookassa_payment_id=''),
                name='unique_nonempty_yookassa_payment_id',
            ),
            models.UniqueConstraint(
                fields=['tenant', 'checkout_client_key'],
                condition=models.Q(checkout_client_key__isnull=False),
                name='unique_tenant_checkout_client_key',
            ),
            models.UniqueConstraint(
                fields=['provider_idempotency_key'],
                condition=~models.Q(provider_idempotency_key=''),
                name='unique_provider_idempotency_key',
            ),
            models.UniqueConstraint(
                fields=['tenant', 'checkout_payload_hash'],
                condition=(
                    models.Q(
                        status='pending',
                        checkout_state__in=(
                            'intent_created',
                            'provider_pending',
                            'provider_created',
                        ),
                    )
                    & ~models.Q(checkout_payload_hash='')
                ),
                name='uniq_active_checkout_payload',
            ),
            models.UniqueConstraint(
                fields=['tenant'],
                condition=models.Q(
                    purchase_type='subscription',
                    status='pending',
                    checkout_state__in=(
                        'intent_created',
                        'provider_pending',
                        'provider_created',
                    ),
                ),
                name='uniq_active_subscription_checkout',
            ),
        ]

    _IMMUTABLE_INTENT_FIELDS = (
        'tenant_id',
        'amount',
        'currency',
        'purchase_type',
        'metadata',
        'checkout_client_key',
        'provider_idempotency_key',
        'checkout_payload_hash',
        'checkout_return_url',
        'entitlement_snapshot',
        'entitlement_plan_id',
        'expected_subscription_version',
    )
    _PROVIDER_PAYMENT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,200}$')

    def save(self, *args, **kwargs):
        """Financial intent fields become immutable once a snapshot exists."""
        original = None
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_INTENT_FIELDS, 'yookassa_payment_id',
            ).first()
            if (
                original is not None
                and original['yookassa_payment_id']
                and self.yookassa_payment_id != original['yookassa_payment_id']
            ):
                raise ValidationError(
                    'Идентификатор платежа YooKassa нельзя изменить после фиксации.',
                )
            if original is not None and original['entitlement_snapshot']:
                changed = [
                    field
                    for field in self._IMMUTABLE_INTENT_FIELDS
                    if getattr(self, field) != original[field]
                ]
                if changed:
                    raise ValidationError(
                        'Нельзя изменять финансовый snapshot Invoice: '
                        + ', '.join(changed),
                    )
        if (
            self.yookassa_payment_id
            and (original is None or not original['yookassa_payment_id'])
            and not self._PROVIDER_PAYMENT_ID_RE.fullmatch(self.yookassa_payment_id)
        ):
            raise ValidationError('Некорректный идентификатор платежа YooKassa.')
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.tenant.slug} — {self.amount}₽ ({self.status})'


class CheckoutIntentKey(TimestampedModel):
    """Immutable mapping of every accepted client UUID to its checkout intent."""

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='checkout_intent_keys',
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='client_keys',
    )
    client_key = models.UUIDField(editable=False)
    checkout_payload_hash = models.CharField(max_length=64, editable=False)

    _IMMUTABLE_FIELDS = (
        'tenant_id',
        'invoice_id',
        'client_key',
        'checkout_payload_hash',
    )

    class Meta:
        verbose_name = 'Ключ checkout intent'
        verbose_name_plural = 'Ключи checkout intent'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'client_key'],
                name='unique_tenant_checkout_intent_key',
            ),
        ]

    def __str__(self):
        return f'{self.tenant_id}: {self.client_key} -> {self.invoice_id}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            original = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_FIELDS,
            ).first()
            if original is not None:
                changed = [
                    field
                    for field in self._IMMUTABLE_FIELDS
                    if getattr(self, field) != original[field]
                ]
                if changed:
                    raise ValidationError(
                        'Нельзя изменять связь client key с checkout intent: '
                        + ', '.join(changed),
                    )
        return super().save(*args, **kwargs)


class BillingOutboxEvent(TimestampedModel):
    """Durable broker-bound side effect created with a billing transaction."""

    EVENT_NOTIFICATION = 'notification'
    EVENT_REQUEUE_LIMIT_REACHED = 'requeue_limit_reached'
    EVENT_TYPE_CHOICES = [
        (EVENT_NOTIFICATION, 'Уведомление'),
        (EVENT_REQUEUE_LIMIT_REACHED, 'Повтор публикации после снятия лимита'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_DISPATCHED = 'dispatched'
    STATUS_DEAD = 'dead'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Ожидает отправки'),
        (STATUS_PROCESSING, 'Отправляется'),
        (STATUS_DISPATCHED, 'Отправлено брокеру'),
        (STATUS_DEAD, 'Требует ручного разбора'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='billing_outbox_events',
    )
    invoice = models.ForeignKey(
        Invoice,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='outbox_events',
    )
    event_type = models.CharField(
        max_length=40,
        choices=EVENT_TYPE_CHOICES,
        editable=False,
    )
    idempotency_key = models.CharField(max_length=200, editable=False)
    payload = models.JSONField(default=dict, editable=False)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    processing_token = models.UUIDField(null=True, blank=True, editable=False)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    _IMMUTABLE_FIELDS = (
        'tenant_id',
        'invoice_id',
        'event_type',
        'idempotency_key',
        'payload',
    )

    class Meta:
        verbose_name = 'Событие billing outbox'
        verbose_name_plural = 'События billing outbox'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'idempotency_key'],
                name='unique_tenant_billing_outbox_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['status', 'next_attempt_at'],
                name='billing_outbox_due_idx',
            ),
        ]

    def __str__(self):
        return f'{self.tenant_id}: {self.event_type} ({self.status})'

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        validate_immutable = (
            self.pk is not None
            and (
                update_fields is None
                or bool(set(update_fields) & set(self._IMMUTABLE_FIELDS))
            )
        )
        if validate_immutable:
            original = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_FIELDS,
            ).first()
            if original is not None:
                changed = [
                    field
                    for field in self._IMMUTABLE_FIELDS
                    if getattr(self, field) != original[field]
                ]
                if changed:
                    raise ValidationError(
                        'Нельзя изменять payload billing outbox: '
                        + ', '.join(changed),
                    )
        return super().save(*args, **kwargs)


class AIUsageLog(TimestampedModel):
    """Детальный лог использования AI-кредитов по дням."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='ai_usage_logs')
    date = models.DateField()
    credits_used = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Лог AI-кредитов'
        verbose_name_plural = 'Логи AI-кредитов'
        unique_together = [('tenant', 'date')]
        indexes = [models.Index(fields=['tenant', '-date'])]

    def __str__(self):
        return f'{self.tenant.slug} — {self.date}: {self.credits_used} кредитов'


class AIWallet(TimestampedModel):
    """AI-баланс тенанта: включённые и отдельно купленные кредиты."""

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name='ai_wallet',
    )
    included_balance = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal('0'),
    )
    included_limit = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal('0'),
    )
    purchased_balance = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal('0'),
    )
    reserved_balance = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal('0'),
    )
    included_expires_at = models.DateTimeField(null=True, blank=True)
    notification_state = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'AI-кошелёк'
        verbose_name_plural = 'AI-кошельки'

    @property
    def total_balance(self):
        return self.included_balance + self.purchased_balance

    @property
    def available_balance(self):
        return max(Decimal('0'), self.total_balance - self.reserved_balance)

    def __str__(self):
        return f'{self.tenant.slug}: {self.available_balance} AI-кредитов'


class AICreditTransaction(TimestampedModel):
    """Неизменяемая проводка AI-кредитов."""

    KIND_GRANT = 'grant'
    KIND_TOPUP = 'topup'
    KIND_RESERVE = 'reserve'
    KIND_RELEASE = 'release'
    KIND_CHARGE = 'charge'
    KIND_EXPIRE = 'expire'
    KIND_ADJUSTMENT = 'adjustment'
    KIND_REFUND = 'refund'
    KIND_CHARGEBACK = 'chargeback'
    KIND_CHOICES = [
        (KIND_GRANT, 'Начисление по подписке'),
        (KIND_TOPUP, 'Покупка кредитов'),
        (KIND_RESERVE, 'Резерв'),
        (KIND_RELEASE, 'Возврат резерва'),
        (KIND_CHARGE, 'Списание'),
        (KIND_EXPIRE, 'Сгорание'),
        (KIND_ADJUSTMENT, 'Корректировка'),
        (KIND_REFUND, 'Возврат платежа'),
        (KIND_CHARGEBACK, 'Чарджбэк'),
    ]

    BALANCE_INCLUDED = 'included'
    BALANCE_PURCHASED = 'purchased'
    BALANCE_RESERVED = 'reserved'
    BALANCE_CHOICES = [
        (BALANCE_INCLUDED, 'Включённый баланс'),
        (BALANCE_PURCHASED, 'Купленный баланс'),
        (BALANCE_RESERVED, 'Резерв'),
    ]

    wallet = models.ForeignKey(
        AIWallet, on_delete=models.CASCADE, related_name='transactions',
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='ai_credit_transactions',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    balance_type = models.CharField(max_length=20, choices=BALANCE_CHOICES)
    amount = models.DecimalField(max_digits=16, decimal_places=4)
    idempotency_key = models.CharField(max_length=160, blank=True)
    reference = models.CharField(max_length=160, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'Проводка AI-кредитов'
        verbose_name_plural = 'Проводки AI-кредитов'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['tenant', 'idempotency_key']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='unique_tenant_ai_credit_idempotency_key',
            ),
        ]


class AICreditPackage(TimestampedModel):
    """Пакет кредитов для разовой покупки через YooKassa."""

    name = models.CharField(max_length=100)
    credits = models.DecimalField(max_digits=14, decimal_places=2)
    price_rub = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        verbose_name = 'Пакет AI-кредитов'
        verbose_name_plural = 'Пакеты AI-кредитов'
        ordering = ['sort_order', 'credits']

    def __str__(self):
        return f'{self.name}: {self.credits} кредитов за {self.price_rub} ₽'


class PaymentReversal(TimestampedModel):
    """Возврат или чарджбэк с результатом обратной кредитной проводки."""

    KIND_REFUND = 'refund'
    KIND_CHARGEBACK = 'chargeback'
    KIND_CHOICES = [
        (KIND_REFUND, 'Возврат'),
        (KIND_CHARGEBACK, 'Чарджбэк'),
    ]

    STATUS_APPLIED = 'applied'
    STATUS_MANUAL_REVIEW = 'manual_review'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_APPLIED, 'Применён'),
        (STATUS_MANUAL_REVIEW, 'Ручная проверка'),
        (STATUS_REJECTED, 'Отклонён'),
    ]

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.PROTECT,
        related_name='reversals',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    provider_reference = models.CharField(max_length=200, unique=True)
    payment_id = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='RUB')
    credits_requested = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        default=Decimal('0'),
    )
    credits_reversed = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        default=Decimal('0'),
    )
    credit_shortfall = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        default=Decimal('0'),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    reason = models.CharField(max_length=500, blank=True)
    processed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Возврат/чарджбэк'
        verbose_name_plural = 'Возвраты и чарджбэки'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['invoice', '-created_at']),
            models.Index(fields=['payment_id', '-created_at']),
        ]


class BillingWebhookEvent(TimestampedModel):
    """Аудит входящего webhook и принятого системой решения."""

    DECISION_RECEIVED = 'received'
    DECISION_APPLIED = 'applied'
    DECISION_IGNORED = 'ignored'
    DECISION_REJECTED = 'rejected'
    DECISION_MANUAL_REVIEW = 'manual_review'
    DECISION_ERROR = 'error'
    DECISION_CHOICES = [
        (DECISION_RECEIVED, 'Получен'),
        (DECISION_APPLIED, 'Применён'),
        (DECISION_IGNORED, 'Игнорирован'),
        (DECISION_REJECTED, 'Отклонён'),
        (DECISION_MANUAL_REVIEW, 'Ручная проверка'),
        (DECISION_ERROR, 'Ошибка'),
    ]

    provider = models.CharField(max_length=30, default='yookassa')
    event_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=200, blank=True)
    payment_id = models.CharField(max_length=200, blank=True)
    idempotency_key = models.CharField(max_length=300, blank=True)
    invoice = models.ForeignKey(
        Invoice,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='webhook_events',
    )
    tenant = models.ForeignKey(
        Tenant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='billing_webhook_events',
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    currency = models.CharField(max_length=3, blank=True)
    decision = models.CharField(
        max_length=20,
        choices=DECISION_CHOICES,
        default=DECISION_RECEIVED,
    )
    reason = models.CharField(max_length=500, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    delivery_count = models.PositiveIntegerField(default=1)
    processing_token = models.UUIDField(null=True, blank=True, editable=False)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    reconciliation_attempts = models.PositiveSmallIntegerField(default=0)
    next_reconciliation_at = models.DateTimeField(null=True, blank=True)
    last_reconciliation_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Webhook биллинга'
        verbose_name_plural = 'Webhook биллинга'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['provider', 'event_type', '-created_at']),
            models.Index(fields=['payment_id', '-created_at']),
            models.Index(fields=['decision', '-created_at']),
            models.Index(
                fields=['decision', 'next_reconciliation_at'],
                name='billing_wh_reconcile_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='unique_provider_billing_webhook_event',
            ),
        ]
