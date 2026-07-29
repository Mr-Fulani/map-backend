from decimal import Decimal

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

    # null = без лимита (Enterprise)
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
    yookassa_subscription_id = models.CharField(
        max_length=200, blank=True, verbose_name='ID подписки ЮKassa',
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата отмены')

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

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Ожидает оплаты'),
        (STATUS_PAID, 'Оплачен'),
        (STATUS_FAILED, 'Ошибка оплаты'),
    ]

    TYPE_SUBSCRIPTION = 'subscription'
    TYPE_AI_TOPUP = 'ai_topup'
    TYPE_CHOICES = [
        (TYPE_SUBSCRIPTION, 'Подписка'),
        (TYPE_AI_TOPUP, 'Пополнение AI-баланса'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='invoices', verbose_name='Тенант',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Сумма, ₽')
    purchase_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES, default=TYPE_SUBSCRIPTION,
        verbose_name='Тип покупки',
    )
    metadata = models.JSONField(default=dict, blank=True, verbose_name='Метаданные')
    status = models.CharField(
        choices=STATUS_CHOICES, default=STATUS_PENDING, max_length=10, verbose_name='Статус',
    )
    yookassa_payment_id = models.CharField(max_length=200, blank=True, verbose_name='ID платежа ЮKassa')
    pdf_s3_key = models.CharField(max_length=500, blank=True, verbose_name='Ключ PDF в S3')
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата оплаты')

    class Meta:
        verbose_name = 'Счёт'
        verbose_name_plural = 'Счета'
        indexes = [models.Index(fields=['tenant', '-created_at'])]

    def __str__(self):
        return f'{self.tenant.slug} — {self.amount}₽ ({self.status})'


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
    purchased_balance = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal('0'),
    )
    reserved_balance = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal('0'),
    )
    included_expires_at = models.DateTimeField(null=True, blank=True)

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
    KIND_CHOICES = [
        (KIND_GRANT, 'Начисление по подписке'),
        (KIND_TOPUP, 'Покупка кредитов'),
        (KIND_RESERVE, 'Резерв'),
        (KIND_RELEASE, 'Возврат резерва'),
        (KIND_CHARGE, 'Списание'),
        (KIND_EXPIRE, 'Сгорание'),
        (KIND_ADJUSTMENT, 'Корректировка'),
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
