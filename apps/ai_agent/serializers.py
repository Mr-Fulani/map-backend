from rest_framework import serializers

from apps.ai_agent.models import AIModel, AIRequestLog


class AIModelSerializer(serializers.ModelSerializer):
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)
    quality_display = serializers.CharField(source='get_quality_tier_display', read_only=True)
    speed_display = serializers.CharField(source='get_speed_tier_display', read_only=True)
    estimated_description_credits = serializers.SerializerMethodField()
    is_configured = serializers.BooleanField(read_only=True)
    is_selectable = serializers.BooleanField(read_only=True)
    availability_reason = serializers.CharField(read_only=True)

    class Meta:
        model = AIModel
        fields = [
            'id', 'provider', 'provider_display', 'external_id', 'display_name',
            'description', 'quality_tier', 'quality_display', 'speed_tier',
            'speed_display', 'supported_tasks', 'input_credits_per_million',
            'cached_input_credits_per_million', 'output_credits_per_million',
            'minimum_credits', 'estimated_description_credits',
            'is_active', 'is_pricing_verified', 'is_configured',
            'is_selectable', 'availability_reason',
        ]

    def get_estimated_description_credits(self, obj) -> str | None:
        if not obj.is_pricing_verified:
            return None
        return str(obj.calculate_credits(input_tokens=2000, output_tokens=800))


class AIRequestLogSerializer(serializers.ModelSerializer):
    task_display = serializers.CharField(source='get_task_type_display', read_only=True)

    class Meta:
        model = AIRequestLog
        fields = [
            'id', 'task_type', 'task_display', 'provider', 'model_id', 'status',
            'input_tokens', 'cached_input_tokens', 'output_tokens',
            'charged_credits', 'duration_ms', 'error_code', 'created_at',
        ]
