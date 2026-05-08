from django.db import models

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class Plan(TimestampedModel):
    """Тарифный план платформы."""

    SLUG_STARTER = 'starter'
    SLUG_BUSINESS = 'business'
    SLUG_PRO = 'pro'
    SLUG_ENTERPRISE = 'enterprise'

    name = models.CharField(max_length=50)
    slug = models.SlugField(unique=True)
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    price_yearly = models.DecimalField(max_digits=10, decimal_places=2)

    # null = без лимита (Enterprise)
    limit_listings = models.PositiveIntegerField(null=True, blank=True)
    limit_sku = models.PositiveIntegerField(null=True, blank=True)
    limit_ai_credits = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

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
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status = models.CharField(choices=STATUS_CHOICES, default=STATUS_TRIAL, max_length=20)
    billing_period = models.CharField(choices=PERIOD_CHOICES, default=PERIOD_MONTHLY, max_length=10)

    current_period_start = models.DateField()
    current_period_end = models.DateField()

    yookassa_subscription_id = models.CharField(max_length=200, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Подписка'
        verbose_name_plural = 'Подписки'

    def __str__(self):
        return f'{self.tenant.slug} — {self.plan.name} ({self.status})'

    @property
    def is_active(self):
        return self.status in (self.STATUS_TRIAL, self.STATUS_ACTIVE)


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

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='invoices')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(choices=STATUS_CHOICES, default=STATUS_PENDING, max_length=10)
    yookassa_payment_id = models.CharField(max_length=200, blank=True)
    pdf_s3_key = models.CharField(max_length=500, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

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
