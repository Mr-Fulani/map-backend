from django.http import JsonResponse


class TenantMiddleware:
    """
    Определяет тенанта по API Key из заголовка или по поддомену.
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
        """Определяет тенанта в порядке приоритета: API Key → поддомен → сессия."""
        from apps.tenants.models import APIKey

        # 1. По API Key из заголовка Authorization: Bearer map_sk_...
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            plaintext = auth_header[7:].strip()
            if plaintext.startswith(APIKey.KEY_PREFIX):
                api_key = APIKey.verify(plaintext)
                if api_key:
                    return api_key.tenant

        # 2. По поддомену (slug.map.domain.ru)
        host = request.get_host().split(':')[0]
        parts = host.split('.')
        if len(parts) >= 3:
            slug = parts[0]
            try:
                from apps.tenants.models import Tenant
                return Tenant.objects.get(slug=slug)
            except Tenant.DoesNotExist:
                pass

        # 3. Для Django Admin — по сессии (tenant в сессии устанавливается при логине)
        if request.path.startswith('/admin/') and hasattr(request, 'user') and request.user.is_authenticated:
            tenant_id = request.session.get('tenant_id')
            if tenant_id:
                try:
                    from apps.tenants.models import Tenant
                    return Tenant.objects.get(pk=tenant_id)
                except Tenant.DoesNotExist:
                    pass

        return None

    def _is_public_path(self, path):
        PUBLIC_PREFIXES = ('/admin/', '/api/docs/', '/api/schema/', '/api/v1/health/')
        return any(path.startswith(p) for p in PUBLIC_PREFIXES)
