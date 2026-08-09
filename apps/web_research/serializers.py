from rest_framework import serializers

from apps.web_research.models import (
    CompetitorOffer, TenantWebResearchSettings, WebResearchClaim,
    WebResearchEvidence, WebResearchRun, WebSearchAttempt,
)
from apps.web_research.search_context import normalize_country_codes, normalized_domains


class TenantWebResearchSettingsSerializer(serializers.ModelSerializer):
    region_label = serializers.CharField(source='get_region_preset_display', read_only=True)

    class Meta:
        model = TenantWebResearchSettings
        fields = [
            'market_research_enabled', 'region_preset', 'region_label',
            'country_codes', 'search_language', 'include_marketplaces',
            'include_used', 'include_preorder', 'include_analogues',
            'exact_matches_only', 'preferred_domains', 'excluded_domains',
            'result_limit', 'price_ttl_hours', 'display_currency',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_country_codes(self, value):
        normalized = normalize_country_codes(value)
        if len(normalized) != len(value or []):
            raise serializers.ValidationError('Используйте уникальные двухбуквенные коды стран.')
        return normalized

    def validate_preferred_domains(self, value):
        return list(normalized_domains(value))

    def validate_excluded_domains(self, value):
        return list(normalized_domains(value))

    def validate_result_limit(self, value):
        if not 1 <= value <= 50:
            raise serializers.ValidationError('Лимит должен быть от 1 до 50.')
        return value

    def validate_price_ttl_hours(self, value):
        if not 1 <= value <= 720:
            raise serializers.ValidationError('Срок актуальности должен быть от 1 до 720 часов.')
        return value

    def validate_display_currency(self, value):
        value = value.upper()
        if value != 'RUB':
            raise serializers.ValidationError('Пока для сравнения поддерживается только RUB.')
        return value

    def validate(self, attrs):
        preset = attrs.get('region_preset', getattr(self.instance, 'region_preset', 'russia'))
        countries = attrs.get('country_codes', getattr(self.instance, 'country_codes', []))
        if preset == TenantWebResearchSettings.RegionPreset.CUSTOM and not countries:
            raise serializers.ValidationError({
                'country_codes': 'Для выбранного региона укажите хотя бы одну страну.',
            })
        return attrs


class WebResearchEvidenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebResearchEvidence
        fields = ['id', 'title', 'url', 'domain', 'query', 'rank', 'provider_id']


class WebSearchAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebSearchAttempt
        fields = [
            'id', 'provider_id', 'query', 'status', 'result_count',
            'duration_ms', 'retryable', 'error_code', 'error_message', 'created_at',
        ]


class WebResearchClaimSerializer(serializers.ModelSerializer):
    evidence_ids = serializers.PrimaryKeyRelatedField(
        source='evidence', many=True, read_only=True,
    )

    class Meta:
        model = WebResearchClaim
        fields = [
            'id', 'claim_type', 'payload', 'confidence', 'evidence_ids',
            'review_status', 'saved_model', 'saved_record_id',
        ]


class CompetitorOfferSerializer(serializers.ModelSerializer):
    match_type_label = serializers.CharField(source='get_match_type_display', read_only=True)
    availability_label = serializers.CharField(source='get_availability_display', read_only=True)
    condition_label = serializers.CharField(source='get_condition_display', read_only=True)
    review_status_label = serializers.CharField(source='get_review_status_display', read_only=True)

    class Meta:
        model = CompetitorOffer
        fields = [
            'id', 'run_id', 'evidence_id', 'provider_id', 'seller_name',
            'domain', 'url', 'country_code', 'region', 'title', 'article',
            'matched_code', 'match_type', 'match_type_label', 'match_confidence',
            'match_reasons', 'price', 'currency', 'normalized_price',
            'normalized_currency', 'is_price_from', 'availability',
            'availability_label', 'availability_text', 'quantity', 'condition',
            'condition_label', 'delivery_text', 'review_status',
            'review_status_label', 'captured_at', 'expires_at', 'created_at',
        ]


class WebResearchRunSerializer(serializers.ModelSerializer):
    evidence = WebResearchEvidenceSerializer(many=True, read_only=True)
    claims = WebResearchClaimSerializer(many=True, read_only=True)
    search_attempts = WebSearchAttemptSerializer(many=True, read_only=True)
    offers = CompetitorOfferSerializer(many=True, read_only=True)

    class Meta:
        model = WebResearchRun
        fields = [
            'id', 'product_id', 'status', 'trigger', 'purpose', 'settings_snapshot',
            'search_provider',
            'ai_provider', 'ai_model', 'queries', 'coverage_before',
            'coverage_after', 'result_count', 'claim_count', 'offer_count', 'generate_after',
            'error_message', 'started_at', 'finished_at', 'created_at',
            'evidence', 'claims',
            'search_attempts',
            'offers',
        ]


class MarketPriceDifferenceSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=16, decimal_places=2)
    percent = serializers.DecimalField(max_digits=12, decimal_places=1)
    direction = serializers.ChoiceField(choices=['above', 'below', 'equal'])


class CatalogMarketOfferSerializer(serializers.Serializer):
    source_id = serializers.CharField()
    source_label = serializers.CharField()
    status = serializers.CharField()
    status_label = serializers.CharField()
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        allow_null=True,
    )
    currency = serializers.CharField(max_length=3)
    price_is_from = serializers.BooleanField()
    availability = serializers.CharField()
    availability_label = serializers.CharField()
    availability_text = serializers.CharField(allow_blank=True)
    quantity = serializers.IntegerField(min_value=0, allow_null=True)
    checked_at = serializers.DateTimeField(allow_null=True)
    url = serializers.URLField(allow_blank=True)
    difference_from_listing = MarketPriceDifferenceSerializer(allow_null=True)
    difference_from_base = MarketPriceDifferenceSerializer(allow_null=True)
    message = serializers.CharField(allow_blank=True)


class MarketCompetitorOfferSerializer(CompetitorOfferSerializer):
    # Explicit strings avoid generating a second set of model-choice enums for
    # this response-only extension of CompetitorOfferSerializer.
    condition = serializers.CharField()
    review_status = serializers.CharField()
    difference_from_base = MarketPriceDifferenceSerializer(allow_null=True)
    difference_from_listing = MarketPriceDifferenceSerializer(allow_null=True)

    class Meta(CompetitorOfferSerializer.Meta):
        fields = CompetitorOfferSerializer.Meta.fields + [
            'difference_from_base',
            'difference_from_listing',
        ]


class MarketStatisticsSerializer(serializers.Serializer):
    minimum = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        allow_null=True,
    )
    median = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        allow_null=True,
    )
    maximum = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        allow_null=True,
    )
    verified_offer_count = serializers.IntegerField(min_value=0)
    available_seller_count = serializers.IntegerField(min_value=0)
    listing_vs_median = MarketPriceDifferenceSerializer(allow_null=True)
    listing_vs_base = MarketPriceDifferenceSerializer(allow_null=True)
    median_vs_base = MarketPriceDifferenceSerializer(allow_null=True)


class MarketRegionSerializer(serializers.Serializer):
    preset = serializers.CharField()
    label = serializers.CharField()
    country_codes = serializers.ListField(child=serializers.CharField(max_length=2))


class MarketFreshnessSerializer(serializers.Serializer):
    last_checked_at = serializers.DateTimeField(allow_null=True)
    ttl_hours = serializers.IntegerField(min_value=1)
    fresh_offer_count = serializers.IntegerField(min_value=0)
    stale_offer_count = serializers.IntegerField(min_value=0)


class ListingMarketComparisonSerializer(serializers.Serializer):
    listing_id = serializers.IntegerField(min_value=1)
    product_id = serializers.IntegerField(min_value=1)
    base_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    listing_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    catalog_offers = CatalogMarketOfferSerializer(many=True)
    internet_offers = MarketCompetitorOfferSerializer(many=True)
    statistics = MarketStatisticsSerializer()
    region = MarketRegionSerializer()
    freshness = MarketFreshnessSerializer()
    active_run = WebResearchRunSerializer(allow_null=True)
    latest_run = WebResearchRunSerializer(allow_null=True)
    warnings = serializers.ListField(child=serializers.CharField())


class WebResearchRunListSerializer(serializers.ModelSerializer):
    product_article = serializers.CharField(source='product.article', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = WebResearchRun
        fields = [
            'id', 'product_id', 'product_article', 'product_name', 'status',
            'trigger', 'purpose', 'settings_snapshot', 'search_provider', 'ai_provider', 'ai_model',
            'coverage_before', 'coverage_after', 'result_count', 'claim_count', 'offer_count',
            'generate_after', 'error_message', 'started_at', 'finished_at',
            'created_at',
        ]
