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


class _OneCCredentialsValidationMixin:
    def validate(self, attrs):
        attrs = super().validate(attrs)
        target_type = attrs.get('type', getattr(self.instance, 'type', None))
        credentials = attrs.get('credentials')

        if target_type in ONEC_TYPES and credentials is not None:
            try:
                attrs['credentials'] = validate_onec_credentials(credentials)
            except OneCCredentialsValidationError as exc:
                raise serializers.ValidationError({
                    'credentials': str(exc),
                }) from exc

        current_type = getattr(self.instance, 'type', None)
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


class DataSourceConnectionSerializer(
    _OneCCredentialsValidationMixin,
    serializers.ModelSerializer,
):
    credentials = CredentialsField(
        write_only=True,
        child=serializers.CharField(max_length=2048, allow_blank=True),
    )

    class Meta:
        model = DataSourceConnection
        fields = ['id', 'name', 'type', 'is_active', 'credentials',
                  'last_sync_at', 'last_sync_status', 'last_error', 'created_at']
        read_only_fields = ['last_sync_at', 'last_sync_status', 'last_error', 'created_at']


class DataSourceConnectionUpdateSerializer(
    _OneCCredentialsValidationMixin,
    serializers.ModelSerializer,
):
    credentials = CredentialsField(
        required=False,
        write_only=True,
        child=serializers.CharField(max_length=2048, allow_blank=True),
    )

    class Meta:
        model = DataSourceConnection
        fields = ['name', 'type', 'is_active', 'credentials']
