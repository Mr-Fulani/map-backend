from rest_framework import serializers

from apps.sync.models import SyncLog


class SyncLogSerializer(serializers.ModelSerializer):
    """Сериализует запись SyncLog для публичного API тенанта."""

    class Meta:
        model = SyncLog
        fields = [
            'id', 'event_type', 'status', 'message',
            'payload', 'created_at', 'product', 'listing',
        ]
        read_only_fields = fields
