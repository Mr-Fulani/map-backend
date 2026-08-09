from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.billing.models import (
    AICreditPackage, AICreditTransaction, AIWallet, BillingOutboxEvent,
    BillingWebhookEvent, CheckoutIntentKey, Invoice, PaymentReversal, Plan,
    Subscription,
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
            'description': (
                'Для AI пустое значение безопасно трактуется как 0, '
                'поэтому у каждого активного тарифа должен быть явный лимит.'
            ),
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
        'ai_period_start', 'ai_period_end',
    ]
    list_filter = ['status', 'plan', 'billing_period']
    search_fields = ['tenant__name', 'tenant__slug']
    readonly_fields = [
        'tenant', 'plan', 'status', 'billing_period',
        'current_period_start', 'current_period_end',
        'ai_period_start', 'ai_period_end', 'yookassa_subscription_id',
        'billing_version', 'cancelled_at', 'created_at', 'updated_at',
    ]
    fieldsets = [
        ('Тенант и тариф', {
            'fields': ['tenant', 'plan', 'status', 'billing_period'],
        }),
        ('Период подписки', {
            'fields': ['current_period_start', 'current_period_end'],
        }),
        ('Период AI-кредитов', {
            'fields': ['ai_period_start', 'ai_period_end'],
        }),
        ('Интеграция ЮKassa', {
            'fields': ['yookassa_subscription_id'],
            'classes': ['collapse'],
        }),
        ('Служебное', {
            'fields': ['billing_version', 'cancelled_at', 'created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    @admin.display(description='Эффективный статус')
    def get_effective_status(self, obj):
        return dict(obj.STATUS_CHOICES)[obj.effective_status]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin):
    """Администрирование счетов на оплату."""

    list_display = [
        'tenant', 'purchase_type', 'amount', 'currency',
        'status', 'refunded_amount', 'refund_review_required',
        'checkout_state', 'reconciliation_attempts', 'paid_at', 'created_at',
    ]
    list_filter = ['status', 'purchase_type', 'refund_review_required']
    search_fields = ['tenant__name', 'yookassa_payment_id']
    readonly_fields = [
        'tenant', 'purchase_type', 'amount', 'currency', 'metadata', 'status',
        'yookassa_payment_id', 'pdf_s3_key', 'paid_at', 'refunded_amount',
        'refund_review_required',
        'checkout_client_key', 'provider_idempotency_key',
        'checkout_payload_hash', 'checkout_return_url',
        'checkout_confirmation_url', 'checkout_state',
        'checkout_attempt_count', 'checkout_first_attempt_at',
        'checkout_last_attempt_at', 'checkout_last_error',
        'entitlement_snapshot', 'entitlement_plan',
        'expected_subscription_version', 'reconciliation_attempts',
        'next_reconciliation_at', 'last_reconciliation_at',
        'created_at', 'updated_at',
    ]
    fieldsets = [
        ('Основное', {
            'fields': [
                'tenant', 'purchase_type', 'amount', 'currency', 'status', 'paid_at',
                'refunded_amount', 'refund_review_required',
            ],
        }),
        ('Интеграция ЮKassa', {
            'fields': [
                'yookassa_payment_id', 'pdf_s3_key', 'metadata',
                'checkout_client_key', 'provider_idempotency_key',
                'checkout_payload_hash', 'checkout_return_url',
                'checkout_confirmation_url', 'checkout_state',
                'checkout_attempt_count', 'checkout_first_attempt_at',
                'checkout_last_attempt_at', 'checkout_last_error',
                'reconciliation_attempts', 'next_reconciliation_at',
                'last_reconciliation_at',
            ],
            'classes': ['collapse'],
        }),
        ('Финансовый snapshot', {
            'fields': [
                'entitlement_snapshot', 'entitlement_plan',
                'expected_subscription_version',
            ],
            'classes': ['collapse'],
        }),
        ('Служебное', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CheckoutIntentKey)
class CheckoutIntentKeyAdmin(ModelAdmin):
    """Read-only registry of canonical and coalesced browser checkout keys."""

    list_display = ['client_key', 'tenant', 'invoice', 'created_at']
    search_fields = [
        'client_key', 'tenant__name', 'tenant__slug', 'invoice__id',
    ]
    readonly_fields = [
        'tenant', 'invoice', 'client_key', 'checkout_payload_hash',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillingOutboxEvent)
class BillingOutboxEventAdmin(ModelAdmin):
    """Read-only operational view of durable broker publications."""

    list_display = [
        'id', 'tenant', 'event_type', 'status', 'attempts',
        'next_attempt_at', 'dispatched_at', 'created_at',
    ]
    list_filter = ['event_type', 'status']
    search_fields = ['id', 'tenant__name', 'tenant__slug', 'idempotency_key']
    readonly_fields = [
        'id', 'tenant', 'invoice', 'event_type', 'idempotency_key', 'payload',
        'status', 'attempts', 'next_attempt_at', 'processing_token',
        'processing_started_at', 'dispatched_at', 'dead_lettered_at', 'last_error',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AIWallet)
class AIWalletAdmin(ModelAdmin):
    list_display = [
        'tenant', 'included_balance', 'purchased_balance',
        'reserved_balance', 'included_limit', 'included_expires_at',
    ]
    search_fields = ['tenant__name', 'tenant__slug']
    readonly_fields = [
        'tenant', 'included_balance', 'purchased_balance', 'reserved_balance',
        'included_limit', 'included_expires_at', 'notification_state',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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


@admin.register(PaymentReversal)
class PaymentReversalAdmin(ModelAdmin):
    list_display = [
        'provider_reference', 'invoice', 'kind', 'amount', 'currency',
        'credits_reversed', 'credit_shortfall', 'status', 'created_at',
    ]
    list_filter = ['kind', 'status', 'currency']
    search_fields = [
        'provider_reference', 'payment_id',
        'invoice__tenant__name', 'invoice__tenant__slug',
    ]
    readonly_fields = [
        'invoice', 'kind', 'provider_reference', 'payment_id',
        'amount', 'currency', 'credits_requested', 'credits_reversed',
        'credit_shortfall', 'status', 'reason', 'processed_at',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillingWebhookEvent)
class BillingWebhookEventAdmin(ModelAdmin):
    list_display = [
        'event_type', 'object_id', 'payment_id', 'invoice',
        'decision', 'delivery_count', 'source_ip', 'created_at',
    ]
    list_filter = ['provider', 'event_type', 'decision']
    search_fields = [
        'object_id', 'payment_id', 'reason',
        'invoice__tenant__name', 'invoice__tenant__slug',
    ]
    readonly_fields = [
        'provider', 'event_type', 'object_id', 'payment_id',
        'idempotency_key', 'invoice', 'tenant', 'amount', 'currency',
        'decision', 'reason', 'payload', 'source_ip', 'delivery_count',
        'processing_token', 'processing_started_at', 'processed_at',
        'reconciliation_attempts', 'next_reconciliation_at',
        'last_reconciliation_at', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
