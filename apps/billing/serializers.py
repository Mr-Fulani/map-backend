from rest_framework import serializers

from apps.billing.models import Invoice, Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'slug',
            'price_monthly', 'price_yearly',
            'limit_listings', 'limit_sku', 'limit_ai_credits',
        ]


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan', 'status', 'billing_period',
            'current_period_start', 'current_period_end',
            'created_at',
        ]


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ['id', 'amount', 'status', 'paid_at', 'created_at']
