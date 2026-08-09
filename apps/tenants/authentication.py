from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class APIKeyAuthentication(BaseAuthentication):
    """Аутентификация по API Key из заголовка Authorization: Bearer <key>."""

    def authenticate(self, request):
        from apps.tenants.models import APIKey

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return None

        plaintext = auth_header[7:].strip()
        if not plaintext.startswith(APIKey.KEY_PREFIX):
            return None

        api_key = APIKey.verify(plaintext)
        if not api_key:
            raise AuthenticationFailed('Недействительный API Key.')

        if not api_key.tenant.is_active:
            raise AuthenticationFailed('Аккаунт заблокирован.')

        # Обновляем last_used_at без лишнего UPDATE если уже сегодня
        if not api_key.last_used_at or api_key.last_used_at.date() < timezone.now().date():
            APIKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())

        # Возвращаем пользователя-владельца (owner) тенанта
        owner_membership = api_key.tenant.members.filter(role='owner').select_related('user').first()
        user = owner_membership.user if owner_membership else None

        return (user, api_key)

    def authenticate_header(self, request):
        return 'Bearer'


class TenantJWTAuthentication(JWTAuthentication):
    """JWT-аутентификация с проверкой актуального tenant membership."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, validated_token = result
        tenant_id = validated_token.get('tenant_id')

        from apps.tenants.models import TenantUser

        if not tenant_id or not TenantUser.objects.filter(
            tenant_id=tenant_id,
            tenant__is_active=True,
            user=user,
        ).exists():
            raise AuthenticationFailed('Доступ к организации отозван.')
        return result
