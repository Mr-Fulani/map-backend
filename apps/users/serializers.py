from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers


class ProfileUpdateSerializer(serializers.Serializer):
    """Сериализатор обновления профиля пользователя (телефон)."""

    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)


class ChangePasswordSerializer(serializers.Serializer):
    """Сериализатор смены пароля."""

    current_password = serializers.CharField(
        max_length=256, write_only=True, trim_whitespace=False,
    )
    new_password = serializers.CharField(
        max_length=256, write_only=True, trim_whitespace=False,
    )

    def validate_new_password(self, value):
        try:
            validate_password(value, user=self.context.get('user'))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class ChangeEmailSerializer(serializers.Serializer):
    """Сериализатор запроса смены email."""

    new_email = serializers.EmailField()
    current_password = serializers.CharField(
        max_length=256, write_only=True, trim_whitespace=False,
    )


class EmailConfirmationSerializer(serializers.Serializer):
    """Одноразовый токен подтверждения, передаваемый только в теле POST."""

    token = serializers.CharField(
        max_length=2048, write_only=True, trim_whitespace=False,
    )


class PasswordResetRequestSerializer(serializers.Serializer):
    """Публичный запрос письма восстановления без раскрытия существования email."""

    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Подтверждение восстановления пароля по одноразовым uid/token."""

    uid = serializers.CharField(max_length=64)
    token = serializers.CharField(max_length=256, trim_whitespace=False)
    new_password = serializers.CharField(
        max_length=256, write_only=True, trim_whitespace=False,
    )

    def validate_uid(self, value):
        try:
            decoded = force_str(urlsafe_base64_decode(value))
            user_id = int(decoded)
        except (TypeError, ValueError, OverflowError):
            raise serializers.ValidationError('Некорректный идентификатор ссылки.') from None
        if user_id <= 0 or len(decoded) > 20:
            raise serializers.ValidationError('Некорректный идентификатор ссылки.')
        return value
