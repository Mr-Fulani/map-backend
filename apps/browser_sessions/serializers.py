from rest_framework import serializers

from apps.browser_sessions.models import BrowserSession
from apps.marketplaces.models import MarketplaceAccount


class BrowserSessionStatusSerializer(serializers.ModelSerializer):
    """Публичное состояние сессии — без credentials и зашифрованных данных."""

    class Meta:
        model = BrowserSession
        fields = ['browser_type', 'session_valid', 'sms_pending', 'last_login_at', 'updated_at']
        read_only_fields = fields


class BrowserLoginStartSerializer(serializers.Serializer):
    """Запрос на начало web-логина."""

    login = serializers.CharField(max_length=100, help_text='Телефон или email от Avito')
    password = serializers.CharField(max_length=200, write_only=True)


class BrowserVerifySerializer(serializers.Serializer):
    """Запрос на передачу SMS/email-кода."""

    code = serializers.CharField(max_length=10)
    method = serializers.ChoiceField(
        choices=[('sms', 'SMS'), ('email', 'Email')],
        required=False,
        default='sms',
        help_text='Метод, которым тенант хочет получить код',
    )


class PublishMethodSerializer(serializers.Serializer):
    """Переключение метода публикации аккаунта."""

    publish_method = serializers.ChoiceField(choices=MarketplaceAccount.PUBLISH_METHOD_CHOICES)
    login = serializers.CharField(max_length=100, required=False)
    password = serializers.CharField(max_length=200, required=False, write_only=True)

    def validate(self, data):
        if data.get('publish_method') == MarketplaceAccount.PUBLISH_WEB:
            if not data.get('login') or not data.get('password'):
                raise serializers.ValidationError(
                    'Для web-режима необходимо передать login и password от Avito'
                )
        return data
