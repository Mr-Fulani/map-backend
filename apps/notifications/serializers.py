from rest_framework import serializers

from apps.notifications.models import TenantNotificationSettings


class NotificationSettingsSerializer(serializers.ModelSerializer):
    """Public tenant notification settings without Telegram credentials."""

    telegram_connected = serializers.SerializerMethodField()

    class Meta:
        model = TenantNotificationSettings
        fields = [
            'telegram_connected',
            'telegram_username',
            'notify_email',
            'notify_on_error',
            'notify_on_critical',
        ]
        read_only_fields = ['telegram_connected', 'telegram_username']

    def get_telegram_connected(self, obj) -> bool:
        return bool(obj.telegram_chat_id)


class NotificationSettingsUpdateSerializer(serializers.ModelSerializer):
    """Bounded, partially-updatable fields accepted by the settings API."""

    class Meta:
        model = TenantNotificationSettings
        fields = ['notify_email', 'notify_on_error', 'notify_on_critical']
        extra_kwargs = {
            'notify_email': {'required': False, 'allow_blank': True},
            'notify_on_error': {'required': False},
            'notify_on_critical': {'required': False},
        }


class NotificationSettingsResponseSerializer(serializers.Serializer):
    status = serializers.CharField(read_only=True)
    data = NotificationSettingsSerializer(read_only=True)  # type: ignore[assignment]
