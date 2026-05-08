from rest_framework import serializers

from apps.datasources.models import DataSourceConnection


class CredentialsField(serializers.DictField):
    """Write-only поле для credentials — в ответе не возвращается."""
    def to_representation(self, value):
        return None


class DataSourceConnectionSerializer(serializers.ModelSerializer):
    credentials = CredentialsField(write_only=True, child=serializers.CharField())

    class Meta:
        model = DataSourceConnection
        fields = ['id', 'name', 'type', 'is_active', 'credentials',
                  'last_sync_at', 'last_sync_status', 'last_error', 'created_at']
        read_only_fields = ['last_sync_at', 'last_sync_status', 'last_error', 'created_at']


class DataSourceConnectionUpdateSerializer(serializers.ModelSerializer):
    credentials = CredentialsField(required=False, write_only=True, child=serializers.CharField())

    class Meta:
        model = DataSourceConnection
        fields = ['name', 'type', 'is_active', 'credentials']
