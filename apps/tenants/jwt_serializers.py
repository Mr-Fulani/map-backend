"""
JWT-сериализаторы с tenant-контекстом.

При login через email/password пользователь получает JWT access+refresh tokens,
в payload которых включён tenant_id и role для быстрого определения контекста
без дополнительных запросов к БД.
"""

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework import serializers


class TenantTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Расширенный TokenObtainPairSerializer, добавляющий tenant_id и role в JWT claims.
    Пользователь может иметь несколько тенантов — берём первый (или указанный).
    """

    tenant_slug = serializers.SlugField(required=False, help_text='Slug тенанта (если у пользователя несколько)')

    def validate(self, attrs):
        # Извлекаем tenant_slug до вызова super(), т.к. super() не знает о нём
        tenant_slug = attrs.pop('tenant_slug', None)
        data = super().validate(attrs)

        # Определяем tenant для пользователя
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
            # Берём первый тенант (по дате создания)
            membership = memberships.first()

        tenant = membership.tenant
        if not tenant.is_active:
            raise serializers.ValidationError(
                {'detail': 'Организация заблокирована.'}
            )

        # Добавляем tenant info в response (не в token)
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

        # Добавляем claims в JWT payload
        from apps.tenants.models import TenantUser
        membership = TenantUser.objects.filter(user=user).select_related('tenant').first()
        if membership:
            token['tenant_id'] = membership.tenant.pk
            token['tenant_slug'] = membership.tenant.slug
            token['role'] = membership.role

        token['email'] = user.email
        return token
