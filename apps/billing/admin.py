from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.billing.models import Invoice, Plan, Subscription


@admin.register(Plan)
class PlanAdmin(ModelAdmin):
    """Администрирование тарифных планов."""

    list_display = ['name', 'slug', 'price_monthly', 'limit_listings', 'limit_sku', 'limit_ai_credits']
    search_fields = ['name']


@admin.register(Subscription)
class SubscriptionAdmin(ModelAdmin):
    """
    Администрирование подписок тенантов.

    Позволяет видеть текущий статус, даты периода и историю.
    """

    list_display = ['tenant', 'plan', 'status', 'current_period_start', 'current_period_end']
    list_filter = ['status', 'plan']
    search_fields = ['tenant__name', 'tenant__slug']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin):
    """Администрирование счетов на оплату."""

    list_display = ['tenant', 'amount', 'status', 'paid_at', 'created_at']
    list_filter = ['status']
    search_fields = ['tenant__name', 'yookassa_payment_id']
    readonly_fields = ['yookassa_payment_id', 'paid_at', 'created_at', 'updated_at']
