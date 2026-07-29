from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.billing.models import (
    AICreditPackage, AICreditTransaction, AIWallet, Invoice, Plan, Subscription,
)


@admin.register(Plan)
class PlanAdmin(ModelAdmin):
    """Администрирование тарифных планов."""

    list_display = ['name', 'slug', 'price_monthly', 'price_yearly', 'limit_listings', 'limit_sku', 'is_active']
    search_fields = ['name', 'slug']
    list_filter = ['is_active']
    fieldsets = [
        ('Основное', {
            'fields': ['name', 'slug', 'is_active'],
        }),
        ('Цены', {
            'fields': ['price_monthly', 'price_yearly'],
        }),
        ('Лимиты', {
            'fields': ['limit_listings', 'limit_sku', 'limit_ai_credits'],
            'description': 'Пустое значение = без лимита (тариф Enterprise).',
        }),
    ]


@admin.register(Subscription)
class SubscriptionAdmin(ModelAdmin):
    """
    Администрирование подписок тенантов.

    Позволяет видеть текущий статус, даты периода и историю.
    """

    list_display = [
        'tenant', 'plan', 'status', 'get_effective_status',
        'billing_period', 'current_period_start', 'current_period_end',
    ]
    list_filter = ['status', 'plan', 'billing_period']
    search_fields = ['tenant__name', 'tenant__slug']
    readonly_fields = ['created_at', 'updated_at', 'cancelled_at']
    fieldsets = [
        ('Тенант и тариф', {
            'fields': ['tenant', 'plan', 'status', 'billing_period'],
        }),
        ('Период подписки', {
            'fields': ['current_period_start', 'current_period_end'],
        }),
        ('Интеграция ЮKassa', {
            'fields': ['yookassa_subscription_id'],
            'classes': ['collapse'],
        }),
        ('Служебное', {
            'fields': ['cancelled_at', 'created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    @admin.display(description='Эффективный статус')
    def get_effective_status(self, obj):
        return dict(obj.STATUS_CHOICES)[obj.effective_status]

    def save_model(self, request, obj, form, change):
        """Subscription — источник истины; legacy-дата тенанта только синхронизируется."""
        super().save_model(request, obj, form, change)
        from apps.billing.services import BillingService
        BillingService.sync_tenant_trial_end(obj)


@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin):
    """Администрирование счетов на оплату."""

    list_display = ['tenant', 'purchase_type', 'amount', 'status', 'paid_at', 'created_at']
    list_filter = ['status', 'purchase_type']
    search_fields = ['tenant__name', 'yookassa_payment_id']
    readonly_fields = ['yookassa_payment_id', 'pdf_s3_key', 'paid_at', 'created_at', 'updated_at']
    fieldsets = [
        ('Основное', {
            'fields': ['tenant', 'purchase_type', 'amount', 'status', 'paid_at'],
        }),
        ('Интеграция ЮKassa', {
            'fields': ['yookassa_payment_id', 'pdf_s3_key', 'metadata'],
            'classes': ['collapse'],
        }),
        ('Служебное', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]


@admin.register(AIWallet)
class AIWalletAdmin(ModelAdmin):
    list_display = [
        'tenant', 'included_balance', 'purchased_balance',
        'reserved_balance', 'included_expires_at',
    ]
    search_fields = ['tenant__name', 'tenant__slug']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(AICreditTransaction)
class AICreditTransactionAdmin(ModelAdmin):
    list_display = ['tenant', 'kind', 'balance_type', 'amount', 'created_at']
    list_filter = ['kind', 'balance_type']
    search_fields = ['tenant__name', 'tenant__slug', 'reference', 'idempotency_key']
    readonly_fields = [
        'wallet', 'tenant', 'kind', 'balance_type', 'amount',
        'idempotency_key', 'reference', 'details', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AICreditPackage)
class AICreditPackageAdmin(ModelAdmin):
    list_display = ['name', 'credits', 'price_rub', 'is_active', 'sort_order']
    list_filter = ['is_active']
