from rest_framework import serializers

from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun,
)


class WebResearchEvidenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebResearchEvidence
        fields = ['id', 'title', 'url', 'domain', 'query', 'rank']


class WebResearchClaimSerializer(serializers.ModelSerializer):
    evidence_ids = serializers.PrimaryKeyRelatedField(
        source='evidence', many=True, read_only=True,
    )

    class Meta:
        model = WebResearchClaim
        fields = [
            'id', 'claim_type', 'payload', 'confidence', 'evidence_ids',
            'saved_model', 'saved_record_id',
        ]


class WebResearchRunSerializer(serializers.ModelSerializer):
    evidence = WebResearchEvidenceSerializer(many=True, read_only=True)
    claims = WebResearchClaimSerializer(many=True, read_only=True)

    class Meta:
        model = WebResearchRun
        fields = [
            'id', 'product_id', 'status', 'trigger', 'search_provider',
            'ai_provider', 'ai_model', 'queries', 'coverage_before',
            'coverage_after', 'result_count', 'claim_count', 'generate_after',
            'error_message', 'started_at', 'finished_at', 'created_at',
            'evidence', 'claims',
        ]
