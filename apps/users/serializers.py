from rest_framework import serializers


class ProfileUpdateSerializer(serializers.Serializer):
    """Сериализатор обновления профиля пользователя (телефон)."""

    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)


class ChangePasswordSerializer(serializers.Serializer):
    """Сериализатор смены пароля."""

    current_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


class ChangeEmailSerializer(serializers.Serializer):
    """Сериализатор запроса смены email."""

    new_email = serializers.EmailField()
