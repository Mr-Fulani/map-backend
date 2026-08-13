from typing import Any

from rest_framework import serializers

from apps.datasources.models import DataSourceConnection
from apps.datasources.validation import (
    ONEC_TYPES,
    OneCCredentialsValidationError,
    validate_onec_credentials,
)


class CredentialsField(serializers.DictField):
    """Write-only поле для credentials — в ответе не возвращается."""
    def to_representation(self, value):
        return None


def _validate_onec_credentials(
    attrs: dict[str, Any],
    instance: object | None,
) -> dict[str, Any]:
    """Validate 1C transitions after DRF's model-level validation."""
    target_type = attrs.get('type', getattr(instance, 'type', None))
    credentials = attrs.get('credentials')

    if target_type in ONEC_TYPES and credentials is not None:
        try:
            attrs['credentials'] = validate_onec_credentials(credentials)
        except OneCCredentialsValidationError as exc:
            raise serializers.ValidationError({
                'credentials': str(exc),
            }) from exc

    current_type = getattr(instance, 'type', None)
    if (
        target_type in ONEC_TYPES
        and current_type is not None
        and current_type not in ONEC_TYPES
        and credentials is None
    ):
        raise serializers.ValidationError({
            'credentials': 'При смене типа на 1С укажите HTTPS URL и учётные данные.',
        })
    return attrs


class DataSourceConnectionSerializer(serializers.ModelSerializer):
    credentials = CredentialsField(
        write_only=True,
        child=serializers.CharField(max_length=2048, allow_blank=True),
    )

    class Meta:
        model = DataSourceConnection
        fields = ['id', 'name', 'type', 'is_active', 'credentials',
                  'last_sync_at', 'last_sync_status', 'last_error', 'created_at']
        read_only_fields = ['last_sync_at', 'last_sync_status', 'last_error', 'created_at']

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return _validate_onec_credentials(attrs, self.instance)


class DataSourceConnectionUpdateSerializer(serializers.ModelSerializer):
    credentials = CredentialsField(
        required=False,
        write_only=True,
        child=serializers.CharField(max_length=2048, allow_blank=True),
    )

    class Meta:
        model = DataSourceConnection
        fields = ['name', 'type', 'is_active', 'credentials']

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return _validate_onec_credentials(attrs, self.instance)
