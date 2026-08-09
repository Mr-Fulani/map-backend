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

        # Истёкшая/отменённая подписка не разлогинивает пользователя: он может
        # просматривать данные и оплатить тариф, но не может менять состояние.
        if (
            request.method not in ('GET', 'HEAD', 'OPTIONS')
            and not self._is_billing_recovery_path(request.path)
        ):
            from apps.billing.models import Subscription
            from apps.billing.services import BillingService

            if BillingService.access_mode(tenant) != Subscription.ACCESS_FULL:
                return JsonResponse(
                    {
                        'status': 'error',
                        'code': 'subscription_inactive',
                        'message': 'Подписка истекла. Продлите её в разделе «Биллинг».',
                    },
                    status=402,
                )

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
                    request._resolved_api_key_id = api_key.pk
                    return api_key.tenant
                return None

            # 2. По JWT token — декодируем прямо здесь, т.к. DRF ещё не запускался
            # Bearer credential никогда не дополняется tenant-ом из Host: это
            # позволяло токену без tenant claim получить чужой request.tenant.
            return self._resolve_from_jwt(plaintext)

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
        """Декодирует JWT и проверяет актуальное членство пользователя в tenant-е."""
        from apps.tenants.models import Tenant, TenantUser
        from apps.users.models import User

        try:
            from rest_framework_simplejwt.tokens import AccessToken
            token = AccessToken(token_string)
            tenant_id = token.get('tenant_id')
            user_id = token.get('user_id')
            auth_version = token.get('auth_version')
            if not tenant_id or not user_id or auth_version is None:
                return None
            user = User.objects.filter(pk=user_id, is_active=True).only('auth_version').first()
            if user is None or user.auth_version != auth_version:
                return None
            if TenantUser.objects.filter(
                tenant_id=tenant_id,
                user_id=user_id,
                tenant__is_active=True,
            ).exists():
                return Tenant.objects.get(pk=tenant_id)
        except Exception:
            pass

        return None

    def _is_public_path(self, path):
        PUBLIC_PREFIXES = (
            '/admin/',
            '/api/docs/',
            '/api/schema/',
            '/api/v1/health/',
            '/api/v1/live/',
            '/api/v1/ready/',
            '/api/v1/auth/register/',
            '/api/v1/auth/token/',
            '/api/v1/auth/browser/',
            '/api/v1/auth/password-reset/',
            '/api/v1/auth/confirm-email/',
            '/api/v1/billing/plans/',
            '/api/v1/billing/webhook/',
            '/api/v1/notifications/webhook/telegram/',
        )
        return any(path.startswith(p) for p in PUBLIC_PREFIXES)

    def _is_billing_recovery_path(self, path):
        """Запись, необходимая для восстановления подписки."""
        return path.startswith((
            '/api/v1/billing/checkout/',
            '/api/v1/auth/change-password/',
            '/api/v1/auth/change-email/',
            '/api/v1/auth/logout/',
            '/api/v1/auth/logout-all/',
        ))
