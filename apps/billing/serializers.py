from rest_framework import serializers

from apps.billing.models import AICreditPackage, Invoice, Plan, Subscription


class CheckoutSerializer(serializers.Serializer):
    """Входные данные для создания платежа YooKassa."""

    plan_slug = serializers.ChoiceField(choices=[
        Plan.SLUG_STARTER, Plan.SLUG_BUSINESS, Plan.SLUG_PRO, Plan.SLUG_ENTERPRISE,
    ])
    period = serializers.ChoiceField(
        choices=[Subscription.PERIOD_MONTHLY, Subscription.PERIOD_YEARLY],
        default=Subscription.PERIOD_MONTHLY,
    )
    return_url = serializers.URLField(required=False)


class PlanSerializer(serializers.ModelSerializer):
    price_yearly_monthly_equivalent = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True,
    )

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'slug',
            'price_monthly', 'price_yearly', 'price_yearly_monthly_equivalent',
            'limit_listings', 'limit_sku', 'limit_ai_credits',
        ]


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    effective_status = serializers.CharField(read_only=True)
    access_mode = serializers.CharField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan', 'status', 'effective_status', 'access_mode', 'billing_period',
            'current_period_start', 'current_period_end',
            'created_at',
        ]


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = [
            'id', 'purchase_type', 'amount', 'currency',
            'status', 'paid_at', 'created_at',
        ]


class AICreditPackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AICreditPackage
        fields = ['id', 'name', 'credits', 'price_rub']


class AITopupCheckoutSerializer(serializers.Serializer):
    package_id = serializers.IntegerField(min_value=1)
    return_url = serializers.URLField(required=False)
