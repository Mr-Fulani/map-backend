from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


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
