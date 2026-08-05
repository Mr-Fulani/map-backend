from rest_framework import serializers

from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun, WebSearchAttempt,
)


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


class WebResearchRunSerializer(serializers.ModelSerializer):
    evidence = WebResearchEvidenceSerializer(many=True, read_only=True)
    claims = WebResearchClaimSerializer(many=True, read_only=True)
    search_attempts = WebSearchAttemptSerializer(many=True, read_only=True)

    class Meta:
        model = WebResearchRun
        fields = [
            'id', 'product_id', 'status', 'trigger', 'search_provider',
            'ai_provider', 'ai_model', 'queries', 'coverage_before',
            'coverage_after', 'result_count', 'claim_count', 'generate_after',
            'error_message', 'started_at', 'finished_at', 'created_at',
            'evidence', 'claims',
            'search_attempts',
        ]


class WebResearchRunListSerializer(serializers.ModelSerializer):
    product_article = serializers.CharField(source='product.article', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = WebResearchRun
        fields = [
            'id', 'product_id', 'product_article', 'product_name', 'status',
            'trigger', 'search_provider', 'ai_provider', 'ai_model',
            'coverage_before', 'coverage_after', 'result_count', 'claim_count',
            'generate_after', 'error_message', 'started_at', 'finished_at',
            'created_at',
        ]
