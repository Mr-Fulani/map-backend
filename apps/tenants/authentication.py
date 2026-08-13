from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
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

        view = (request.parser_context or {}).get('view')
        if (
            view is None
            or view.__class__.__dict__.get('api_key_enabled') is not True
        ):
            raise PermissionDenied('API Key не разрешён для этого endpoint.')
        scope_policy = getattr(view, 'api_key_scopes', None)
        required_scopes = (
            scope_policy.get(request.method)
            if isinstance(scope_policy, dict)
            else None
        )
        if required_scopes is None:
            raise PermissionDenied('API Key не разрешён для этого endpoint.')

        from apps.tenants.principals import APIKeyPrincipal

        principal = APIKeyPrincipal.from_api_key(api_key)
        tenant = getattr(request, 'tenant', None)
        if tenant is None or principal.tenant_id != tenant.pk:
            raise PermissionDenied('API Key не принадлежит текущей организации.')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not principal.can_write():
            raise PermissionDenied('Viewer API Key не может изменять данные.')
        if not principal.has_scopes(required_scopes):
            raise PermissionDenied('API Key не имеет необходимого scope.')

        return (principal, api_key)

    def authenticate_header(self, request):
        return 'Bearer'


class TenantJWTAuthentication(JWTAuthentication):
    """JWT-аутентификация с проверкой актуального tenant membership."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, validated_token = result
        from apps.users.models import User
        if not isinstance(user, User):
            # The configured authentication backend must never return a
            # different user model: fail closed instead of trusting claims.
            raise AuthenticationFailed('Некорректный пользователь сессии.')
        tenant_id = validated_token.get('tenant_id')
        token_auth_version = validated_token.get('auth_version')

        if token_auth_version != user.auth_version:
            raise AuthenticationFailed('Сессия отозвана. Войдите снова.')

        from apps.tenants.models import TenantUser

        if not tenant_id or not TenantUser.objects.filter(
            tenant_id=tenant_id,
            tenant__is_active=True,
            user=user,
        ).exists():
            raise AuthenticationFailed('Доступ к организации отозван.')
        return user, validated_token
