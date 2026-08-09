from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers

from apps.billing.models import AICreditPackage, Invoice, Plan, Subscription


def _origin_identity(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(str(value or '').strip())
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except (TypeError, ValueError):
        return None
    return parsed.scheme.lower(), parsed.hostname.rstrip('.').lower(), port


def validate_billing_return_url(value: str) -> str:
    """Allow YooKassa redirects only to explicitly trusted application origins."""
    candidate = _origin_identity(value)
    allowed = {
        origin
        for origin in (
            _origin_identity(item)
            for item in settings.BILLING_RETURN_URL_ALLOWED_ORIGINS
        )
        if origin is not None
    }
    if candidate is None or candidate not in allowed:
        raise serializers.ValidationError(
            'return_url должен принадлежать доверенному frontend origin.',
        )
    return value


class CheckoutSerializer(serializers.Serializer):
    """Входные данные для создания платежа YooKassa."""

    idempotency_key = serializers.UUIDField()
    plan_slug = serializers.ChoiceField(choices=[
        Plan.SLUG_STARTER, Plan.SLUG_BUSINESS, Plan.SLUG_PRO, Plan.SLUG_ENTERPRISE,
    ])
    period = serializers.ChoiceField(
        choices=[Subscription.PERIOD_MONTHLY, Subscription.PERIOD_YEARLY],
        default=Subscription.PERIOD_MONTHLY,
    )
    return_url = serializers.URLField(required=False, validators=[validate_billing_return_url])


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
            'ai_period_start', 'ai_period_end',
            'created_at',
        ]


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = [
            'id', 'purchase_type', 'amount', 'currency',
            'status', 'paid_at', 'refunded_amount',
            'refund_review_required', 'created_at',
        ]


class AICreditPackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AICreditPackage
        fields = ['id', 'name', 'credits', 'price_rub']


class AITopupCheckoutSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField()
    package_id = serializers.IntegerField(min_value=1)
    return_url = serializers.URLField(required=False, validators=[validate_billing_return_url])
