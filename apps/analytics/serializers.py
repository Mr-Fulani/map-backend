"""OpenAPI contract for the tenant dashboard summary."""

from rest_framework import serializers


class DashboardLimitSerializer(serializers.Serializer):
    used = serializers.IntegerField()
    limit = serializers.IntegerField(allow_null=True)


class DashboardAIUsageSerializer(serializers.Serializer):
    used = serializers.DecimalField(max_digits=16, decimal_places=4)
    successful_requests = serializers.IntegerField()
    limit = serializers.DecimalField(max_digits=16, decimal_places=4)
    included_balance = serializers.DecimalField(max_digits=16, decimal_places=4)
    included_percent_used = serializers.DecimalField(max_digits=7, decimal_places=2)
    purchased_balance = serializers.DecimalField(max_digits=16, decimal_places=4)
    reserved_balance = serializers.DecimalField(max_digits=16, decimal_places=4)
    total_balance = serializers.DecimalField(max_digits=16, decimal_places=4)
    available_balance = serializers.DecimalField(max_digits=16, decimal_places=4)
    included_expires_at = serializers.DateTimeField(allow_null=True)
    unlimited = serializers.BooleanField()
    individual_limit = serializers.BooleanField()
    overage_active = serializers.BooleanField()
    threshold = serializers.ChoiceField(
        choices=['normal', 'warning', 'critical', 'exhausted'],
    )


class DashboardUsageSerializer(serializers.Serializer):
    listings = DashboardLimitSerializer()
    sku = DashboardLimitSerializer()
    ai_credits = DashboardAIUsageSerializer()


class DashboardSubscriptionSerializer(serializers.Serializer):
    plan = serializers.CharField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    access_mode = serializers.CharField(allow_null=True)
    current_period_end = serializers.DateField(allow_null=True)
    current_period_days_left = serializers.IntegerField(allow_null=True)
    grace_days_left = serializers.IntegerField(allow_null=True)


class DashboardAttentionSerializer(serializers.Serializer):
    code = serializers.CharField()
    severity = serializers.ChoiceField(choices=['info', 'warning', 'critical'])
    title = serializers.CharField()
    message = serializers.CharField()
    count = serializers.IntegerField()
    href = serializers.CharField(allow_null=True)
    metadata = serializers.JSONField()


class DashboardAnalyticsSummarySerializer(serializers.Serializer):
    views = serializers.IntegerField()
    contacts = serializers.IntegerField()
    impressions = serializers.IntegerField()
    avg_ctr = serializers.FloatField()
    active_listings = serializers.IntegerField()


class DashboardAnalyticsDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    views = serializers.IntegerField()
    contacts = serializers.IntegerField()
    impressions = serializers.IntegerField()


class DashboardAnalyticsSerializer(serializers.Serializer):
    period_days = serializers.IntegerField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    summary = DashboardAnalyticsSummarySerializer()
    daily = DashboardAnalyticsDaySerializer(many=True)


class DashboardFunnelSerializer(serializers.Serializer):
    products = serializers.IntegerField()
    listings = serializers.IntegerField()
    active_listings = serializers.IntegerField()
    queued_listings = serializers.IntegerField()
    pending_listings = serializers.IntegerField()
    rejected_listings = serializers.IntegerField()
    requires_review_listings = serializers.IntegerField()
    limit_reached_listings = serializers.IntegerField()


class DashboardActivitySerializer(serializers.Serializer):
    code = serializers.CharField()
    severity = serializers.ChoiceField(choices=['error', 'warning', 'success', 'info'])
    title = serializers.CharField()
    message = serializers.CharField()
    occurred_at = serializers.DateTimeField()
    product_id = serializers.IntegerField(allow_null=True)
    listing_id = serializers.IntegerField(allow_null=True)
    href = serializers.CharField()
    metadata = serializers.JSONField()


class DashboardDatasourceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    type = serializers.CharField()
    is_active = serializers.BooleanField()
    last_sync_at = serializers.DateTimeField(allow_null=True)
    last_sync_status = serializers.CharField()
    last_error = serializers.CharField(allow_blank=True)


class DashboardDatasourceIssueSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    last_sync_at = serializers.DateTimeField(allow_null=True)
    message = serializers.CharField()


class DashboardDatasourcesSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField()
    healthy = serializers.IntegerField()
    # Public contract deliberately uses ``errors``; DRF's base Serializer has
    # a read-only property with the same name, so the typing plugin needs the
    # explicit override while runtime field binding remains standard DRF.
    errors = serializers.IntegerField()  # type: ignore[assignment]
    never_synced = serializers.IntegerField()
    latest_sync_at = serializers.DateTimeField(allow_null=True)
    returned_count = serializers.IntegerField()
    truncated = serializers.BooleanField()
    items = DashboardDatasourceSerializer(many=True)
    latest_issues = DashboardDatasourceIssueSerializer(many=True)


class DashboardAvitoAccountSerializer(serializers.Serializer):
    account_id = serializers.IntegerField()
    account_name = serializers.CharField()
    is_active = serializers.BooleanField()
    connection_status = serializers.CharField()
    autoload_status = serializers.CharField()
    feed_configured = serializers.BooleanField(allow_null=True)
    profile_stale = serializers.BooleanField()
    tariff_status = serializers.CharField()
    tariff_stale = serializers.BooleanField()
    subscription_ends_at = serializers.DateField(allow_null=True)
    subscription_source = serializers.ChoiceField(
        choices=['avito_tariff', 'manual', 'unavailable'],
    )
    days_left = serializers.IntegerField(allow_null=True)
    placements_remaining = serializers.IntegerField(allow_null=True)
    placements_total = serializers.IntegerField(allow_null=True)
    last_error_code = serializers.CharField(allow_blank=True)
    last_error_message = serializers.CharField(allow_blank=True)


class DashboardMarketplacesSerializer(serializers.Serializer):
    avito_total = serializers.IntegerField()
    avito_truncated = serializers.BooleanField()
    avito = DashboardAvitoAccountSerializer(many=True)


class DashboardServiceUsageSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    status = serializers.ChoiceField(choices=['coming_soon', 'available', 'unavailable'])
    used = serializers.DecimalField(
        max_digits=16,
        decimal_places=4,
        allow_null=True,
    )
    limit = serializers.DecimalField(
        max_digits=16,
        decimal_places=4,
        allow_null=True,
    )
    unit = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    uses_shared_ai_balance = serializers.BooleanField()
    href = serializers.CharField(allow_null=True)
    metadata = serializers.JSONField()


class DashboardServicesSerializer(serializers.Serializer):
    image_processing = DashboardServiceUsageSerializer()


class DashboardSummarySerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    subscription = DashboardSubscriptionSerializer()
    usage = DashboardUsageSerializer()
    attention = DashboardAttentionSerializer(many=True)
    analytics = DashboardAnalyticsSerializer()
    funnel = DashboardFunnelSerializer()
    activity = DashboardActivitySerializer(many=True)
    datasources = DashboardDatasourcesSerializer()
    marketplaces = DashboardMarketplacesSerializer()
    services = DashboardServicesSerializer()


class DashboardSummaryResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = DashboardSummarySerializer()  # type: ignore[assignment]
