from django.http import JsonResponse


class TenantMiddleware:
    """
    Определяет тенанта по API Key из заголовка, JWT claims, или по поддомену.
    Устанавливает request.tenant для всех последующих обработчиков.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Пути, не требующие тенанта
        if self._is_public_path(request.path):
            request.tenant = None
            return self.get_response(request)

        tenant = self._resolve_tenant(request)

        if tenant is None:
            # Тенант не определён — продолжаем без него (DRF-аутентификация сама вернёт 403)
            request.tenant = None
            return self.get_response(request)

        if not tenant.is_active:
            return JsonResponse(
                {'status': 'error', 'code': 'tenant_inactive', 'message': 'Аккаунт заблокирован'},
                status=403,
            )

        request.tenant = tenant
        return self.get_response(request)

    def _resolve_tenant(self, request):
        """Определяет тенанта в порядке приоритета: API Key → JWT → поддомен → сессия."""
        from apps.tenants.models import APIKey, Tenant

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if auth_header.startswith('Bearer '):
            plaintext = auth_header[7:].strip()

            # 1. По API Key (map_sk_...)
            if plaintext.startswith(APIKey.KEY_PREFIX):
                api_key = APIKey.verify(plaintext)
                if api_key:
                    return api_key.tenant

            # 2. По JWT token — декодируем прямо здесь, т.к. DRF ещё не запускался
            else:
                tenant = self._resolve_from_jwt(plaintext)
                if tenant:
                    return tenant

        # 3. По поддомену (slug.map.domain.ru)
        host = request.get_host().split(':')[0]
        parts = host.split('.')
        if len(parts) >= 3:
            slug = parts[0]
            try:
                return Tenant.objects.get(slug=slug)
            except Tenant.DoesNotExist:
                pass

        # 4. Для Django Admin — по сессии
        if request.path.startswith('/admin/') and hasattr(request, 'user') and request.user.is_authenticated:
            tenant_id = request.session.get('tenant_id')
            if tenant_id:
                try:
                    return Tenant.objects.get(pk=tenant_id)
                except Tenant.DoesNotExist:
                    pass

        return None

    def _resolve_from_jwt(self, token_string):
        """Декодирует JWT и извлекает tenant_id из claims."""
        from apps.tenants.models import Tenant

        try:
            from rest_framework_simplejwt.tokens import AccessToken
            token = AccessToken(token_string)
            tenant_id = token.get('tenant_id')
            if tenant_id:
                return Tenant.objects.get(pk=tenant_id)
        except Exception:
            pass

        # Fallback: если tenant_id нет в claims, ищем по user_id
        try:
            from rest_framework_simplejwt.tokens import AccessToken
            token = AccessToken(token_string)
            user_id = token.get('user_id')
            if user_id:
                from apps.tenants.models import TenantUser
                membership = TenantUser.objects.filter(
                    user_id=user_id
                ).select_related('tenant').first()
                if membership:
                    return membership.tenant
        except Exception:
            pass

        return None

    def _is_public_path(self, path):
        PUBLIC_PREFIXES = (
            '/admin/',
            '/api/docs/',
            '/api/schema/',
            '/api/v1/health/',
            '/api/v1/auth/register/',
            '/api/v1/auth/token/',
            '/api/v1/billing/plans/',
            '/api/v1/billing/webhook/',
            '/api/v1/notifications/webhook/telegram/',
        )
        return any(path.startswith(p) for p in PUBLIC_PREFIXES)
