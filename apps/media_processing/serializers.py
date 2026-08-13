from django.core.files.storage import default_storage
from rest_framework import serializers

from apps.media_processing.models import (
    ImageAssessment,
    MediaProcessingJob,
    MediaProcessingPreset,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.media_processing.providers.base import MediaOperation
from apps.media_processing.providers.registry import MediaProviderUnavailable
from apps.media_processing.services import resolve_provider_for_request


class MediaProcessingPresetSerializer(serializers.ModelSerializer):
    scope = serializers.SerializerMethodField()

    class Meta:
        model = MediaProcessingPreset
        fields = [
            'id', 'name', 'slug', 'operations', 'parameters',
            'provider_preferences', 'is_active', 'is_default', 'scope',
        ]
        read_only_fields = ['scope']

    def get_scope(self, obj) -> str:
        return 'tenant' if obj.tenant_id else 'platform'

    def validate_operations(self, value):
        try:
            normalized = [MediaOperation(operation).value for operation in value]
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(str(exc)) from exc
        if len(normalized) != len(set(normalized)):
            raise serializers.ValidationError('Операции не должны повторяться.')
        return normalized

    def validate_provider_preferences(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Ожидается список провайдеров.')
        normalized = []
        for provider_id in value:
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise serializers.ValidationError('provider_id должен быть непустой строкой.')
            normalized.append(provider_id.strip().lower())
        if len(normalized) != len(set(normalized)):
            raise serializers.ValidationError('Провайдеры не должны повторяться.')
        return normalized

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get('request')
        operations = attrs.get('operations', getattr(self.instance, 'operations', []))
        preferences = attrs.get(
            'provider_preferences',
            getattr(self.instance, 'provider_preferences', []),
        )
        if not request or not preferences or not operations:
            return attrs
        normalized_operations = tuple(MediaOperation(value) for value in operations)
        for provider_id in preferences:
            try:
                resolve_provider_for_request(
                    request.tenant,
                    normalized_operations,
                    provider_id=provider_id,
                )
            except MediaProviderUnavailable as exc:
                raise serializers.ValidationError({
                    'provider_preferences': [str(exc)],
                }) from exc
        return attrs


class ProductImageVariantSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImageVariant
        fields = [
            'id', 'product_image', 'job', 'provider_id', 'operations',
            'parameters', 'url', 'content_type', 'width', 'height',
            'file_size_kb', 'sha256', 'is_active', 'created_at',
        ]
        read_only_fields = fields

    def get_url(self, obj) -> str:
        url = default_storage.url(obj.s3_key)
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request and url.startswith('/') else url


class MediaProcessingJobSerializer(serializers.ModelSerializer):
    variants = ProductImageVariantSerializer(many=True, read_only=True)

    class Meta:
        model = MediaProcessingJob
        fields = [
            'id', 'product_image', 'preset', 'operations', 'parameters',
            'provider_id', 'provider_job_id', 'status', 'idempotency_key',
            'estimated_credits', 'charged_credits', 'error_code', 'error_message',
            'started_at', 'finished_at', 'created_at', 'variants',
        ]
        read_only_fields = fields


class MediaJobCreateSerializer(serializers.Serializer):
    preset_id = serializers.IntegerField(required=False, allow_null=True)
    operations = serializers.ListField(
        child=serializers.ChoiceField(choices=[operation.value for operation in MediaOperation]),
        required=False,
        max_length=len(MediaOperation),
    )
    parameters = serializers.DictField(required=False)
    provider_id = serializers.SlugField(required=False, allow_blank=True)
    idempotency_key = serializers.UUIDField(required=True)

    def validate_operations(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError('Операции не должны повторяться.')
        return value

    def validate(self, attrs):
        if not attrs.get('preset_id') and not attrs.get('operations'):
            raise serializers.ValidationError('Укажите preset_id или operations.')
        return attrs


class TenantMediaSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantMediaSettings
        fields = [
            'default_preset', 'provider_preferences',
            'auto_process_manual_uploads', 'auto_process_approved_search',
            'allow_generative_operations',
        ]

    def validate_default_preset(self, preset):
        request = self.context.get('request')
        if preset and request and preset.tenant_id not in (None, request.tenant.pk):
            raise serializers.ValidationError('Пресет принадлежит другому тенанту.')
        if preset and not preset.is_active:
            raise serializers.ValidationError('Выбранный пресет отключён.')
        return preset


class ImageAssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageAssessment
        fields = [
            'id', 'product', 'product_image', 'source_url', 'source_id',
            'provider_id', 'model_id', 'verdict', 'score', 'reason_codes',
            'checks', 'expected_product', 'created_at',
        ]
        read_only_fields = fields
