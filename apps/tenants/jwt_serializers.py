"""
JWT-сериализаторы с tenant-контекстом.

При login через email/password пользователь получает JWT access+refresh tokens,
в payload которых включён tenant_id и role для быстрого определения контекста
без дополнительных запросов к БД.
"""

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework import serializers

from apps.tenants.session_tokens import rotate_refresh_token


class TenantTokenTenantSerializer(serializers.Serializer):
    """Организация, выбранная при входе."""

    id = serializers.IntegerField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    name = serializers.CharField(read_only=True)


class TenantTokenUserSerializer(serializers.Serializer):
    """Краткие данные вошедшего пользователя."""

    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)


class TenantTokenObtainPairResponseSerializer(serializers.Serializer):
    """Точный payload успешного tenant-aware JWT login."""

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)
    browser_session_id = serializers.CharField(read_only=True)
    tenant = TenantTokenTenantSerializer(read_only=True)
    role = serializers.CharField(read_only=True)
    user = TenantTokenUserSerializer(read_only=True)


class BrowserTokenObtainPairResponseSerializer(serializers.Serializer):
    """Browser login payload; refresh intentionally exists only in HttpOnly cookie."""

    access = serializers.CharField(read_only=True)
    browser_session_id = serializers.CharField(read_only=True)
    tenant = TenantTokenTenantSerializer(read_only=True)
    role = serializers.CharField(read_only=True)
    user = TenantTokenUserSerializer(read_only=True)


class BrowserTokenRefreshResponseSerializer(serializers.Serializer):
    """Browser refresh payload; rotated refresh remains in HttpOnly cookie."""

    access = serializers.CharField(read_only=True)
    browser_session_id = serializers.CharField(read_only=True)


class TenantTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Расширенный TokenObtainPairSerializer, добавляющий tenant_id и role в JWT claims.
    Пользователь может иметь несколько тенантов — берём первый (или указанный).
    """

    password = serializers.CharField(
        max_length=256,
        write_only=True,
        trim_whitespace=False,
    )
    tenant_slug = serializers.SlugField(
        required=False,
        max_length=50,
        help_text='Slug тенанта (если у пользователя несколько)',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # SimpleJWT replaces these fields in its ``__init__`` without size
        # limits, so class-level declarations alone would not bound parsing.
        self.fields[self.username_field] = serializers.EmailField(
            max_length=254,
            write_only=True,
        )
        self.fields['password'] = serializers.CharField(
            max_length=256,
            write_only=True,
            trim_whitespace=False,
            style={'input_type': 'password'},
        )

    def validate(self, attrs):
        # Извлекаем tenant_slug до вызова super(), т.к. super() не знает о нём
        tenant_slug = attrs.pop('tenant_slug', None)

        # 1. Аутентифицируем пользователя через базовый класс (проверяет email/password)
        # super() вызывается через TokenObtainPairSerializer, чтобы пока не генерировать токены
        data = super(TokenObtainPairSerializer, self).validate(attrs)

        # 2. Теперь self.user доступен, определяем его тенант
        from apps.tenants.models import TenantUser
        memberships = TenantUser.objects.filter(
            user=self.user
        ).select_related('tenant')

        if not memberships.exists():
            raise serializers.ValidationError(
                {'detail': 'У пользователя нет привязанных организаций.'}
            )

        if tenant_slug:
            membership = memberships.filter(tenant__slug=tenant_slug).first()
            if not membership:
                raise serializers.ValidationError(
                    {'detail': f'Организация "{tenant_slug}" не найдена или вы не являетесь её участником.'}
                )
        else:
            if memberships.count() != 1:
                raise serializers.ValidationError({
                    'tenant_slug': 'Укажите организацию для входа.',
                })
            membership = memberships.first()

        tenant = membership.tenant
        if not tenant.is_active:
            raise serializers.ValidationError(
                {'detail': 'Организация заблокирована.'}
            )

        # 3. Сохраняем membership в user, чтобы get_token мог его использовать
        self.user._current_tenant_membership = membership

        # 4. Генерируем токены с правильным payload
        refresh = self.get_token(self.user)

        data["refresh"] = str(refresh)
        data["access"] = str(refresh.access_token)
        data['browser_session_id'] = refresh['sid']

        # 5. Добавляем tenant info в response
        data['tenant'] = {
            'id': tenant.pk,
            'slug': tenant.slug,
            'name': tenant.name,
        }
        data['role'] = membership.role
        data['user'] = {
            'id': self.user.pk,
            'email': self.user.email,
        }

        return data

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Добавляем claims в JWT payload из сохраненного контекста
        membership = getattr(user, '_current_tenant_membership', None)
        if membership is None or membership.user_id != user.pk:
            raise ValueError('Tenant membership must be selected before issuing JWT.')

        token['tenant_id'] = membership.tenant.pk
        token['tenant_slug'] = membership.tenant.slug
        token['role'] = membership.role

        token['email'] = user.email
        token['auth_version'] = user.auth_version
        # Stable across refresh rotation (whose JTI changes every time) and safe
        # to expose as metadata: this value is not a bearer credential.
        token['sid'] = token['jti']
        # SimpleJWT сохраняет OutstandingToken внутри ``for_user`` до того,
        # как tenant claims добавлены этим сериализатором. Синхронизируем
        # audit/blacklist запись с фактически выданным refresh token.
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        OutstandingToken.objects.filter(
            user=user,
            jti=token['jti'],
        ).update(token=str(token))
        return token


class TenantTokenRefreshSerializer(TokenRefreshSerializer):
    """Не обновляет токен после удаления пользователя из tenant-а."""

    refresh = serializers.CharField(
        max_length=4096,
        write_only=True,
        trim_whitespace=False,
    )

    def validate(self, attrs):
        return rotate_refresh_token(attrs['refresh'])


class LogoutSerializer(serializers.Serializer):
    """Refresh token, который следует отозвать при CLI/API logout."""

    refresh = serializers.CharField(
        max_length=4096,
        write_only=True,
        trim_whitespace=False,
    )
