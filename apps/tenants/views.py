import hmac
import hashlib
import json

import requests
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import (
    APIKey, CatalogDomain, TenantCatalogDomain, WEBHOOK_EVENTS, WebhookEndpoint,
)
from apps.tenants.serializers import (
    APIKeyCreateSerializer,
    APIKeySerializer,
    CatalogDomainSerializer,
    RegisterSerializer,
    TenantSerializer,
    TenantUserSerializer,
    WebhookEndpointSerializer,
    WebhookEndpointWriteSerializer,
)
from apps.tenants.services import APIKeyService, TenantService


@extend_schema(tags=['Auth'])
class RegisterView(APIView):
    """POST /api/v1/auth/register/ — создать тенанта и получить API Key."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tenant, plaintext = TenantService.create_tenant(
            name=serializer.validated_data['name'],
            slug=serializer.validated_data['slug'],
            owner_email=serializer.validated_data['email'],
            owner_password=serializer.validated_data['password'],
        )

        return Response({
            'status': 'ok',
            'data': {
                'tenant': TenantSerializer(tenant).data,
                # Показываем plaintext ключ только один раз
                'api_key': plaintext,
                'warning': 'Сохраните API Key — он больше не будет показан.',
            },
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Tenant'])
class TenantDetailView(APIView):
    """GET /api/v1/tenant/ — информация о текущем тенанте."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'status': 'ok',
            'data': TenantSerializer(request.tenant).data,
        })


@extend_schema(tags=['Tenant'])
class CatalogDomainListView(APIView):
    """GET/POST /api/v1/catalog-domains/ — platform-домены для dashboard."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        domains = CatalogDomain.objects.filter(is_active=True).exclude(
            slug__in=['mixed', 'unknown'],
        ).order_by('sort_order', 'name')
        enabled_ids = set(
            TenantCatalogDomain.objects.filter(
                tenant=request.tenant,
                is_enabled=True,
            ).values_list('domain_id', flat=True)
        )
        return Response({
            'status': 'ok',
            'data': CatalogDomainSerializer(
                domains,
                many=True,
                context={'tenant': request.tenant, 'enabled_domain_ids': enabled_ids},
            ).data,
        })

    def post(self, request):
        domain_slug = str(request.data.get('domain_slug') or '').strip()
        is_enabled = request.data.get('is_enabled')
        if isinstance(is_enabled, str):
            is_enabled = is_enabled.lower() == 'true'
        else:
            is_enabled = bool(is_enabled)
        domain = CatalogDomain.objects.filter(
            slug=domain_slug,
            is_active=True,
        ).exclude(slug__in=['mixed', 'unknown']).first()
        if domain is None:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Корневая категория не найдена'},
                status=status.HTTP_404_NOT_FOUND,
            )

        enabling, _ = TenantCatalogDomain.objects.update_or_create(
            tenant=request.tenant,
            domain=domain,
            defaults={'is_enabled': is_enabled},
        )
        if is_enabled:
            from apps.products.services import ProductCategorySeedService
            ProductCategorySeedService.seed_tenant_default_categories(request.tenant, domain)

        return Response({
            'status': 'ok',
            'data': {
                'domain_slug': domain.slug,
                'is_enabled': enabling.is_enabled,
            },
        })


@extend_schema(tags=['Tenant'])
class TenantUserListView(APIView):
    """GET /api/v1/tenant/users/ — список пользователей тенанта."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        members = request.tenant.members.select_related('user').all()
        return Response({
            'status': 'ok',
            'data': TenantUserSerializer(members, many=True).data,
        })


@extend_schema(tags=['API Keys'])
class APIKeyListView(APIView):
    """GET /api/v1/tenant/api-keys/ — список API ключей. POST — создать новый."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        keys = APIKey.objects.filter(tenant=request.tenant)
        return Response({
            'status': 'ok',
            'data': APIKeySerializer(keys, many=True).data,
        })

    def post(self, request):
        serializer = APIKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        api_key, plaintext = APIKeyService.create_key(
            tenant=request.tenant,
            name=serializer.validated_data['name'],
        )

        return Response({
            'status': 'ok',
            'data': {
                **APIKeySerializer(api_key).data,
                'key': plaintext,
                'warning': 'Сохраните API Key — он больше не будет показан.',
            },
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['API Keys'])
class APIKeyRevokeView(APIView):
    """DELETE /api/v1/tenant/api-keys/{id}/ — отозвать ключ."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, key_id):
        APIKeyService.revoke_key(key_id=key_id, tenant=request.tenant)
        return Response({'status': 'ok'}, status=status.HTTP_200_OK)


@extend_schema(tags=['Webhooks'])
class WebhookEndpointListView(APIView):
    """GET /api/v1/webhooks/ — список вебхуков. POST — создать."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Возвращает вебхук-эндпоинты текущего тенанта."""
        qs = WebhookEndpoint.objects.filter(tenant=request.tenant).order_by('-created_at')
        return Response({'status': 'ok', 'data': WebhookEndpointSerializer(qs, many=True).data})

    def post(self, request):
        """Создаёт новый вебхук-эндпоинт с автоматически сгенерированным секретом."""
        serializer = WebhookEndpointWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        endpoint = WebhookEndpoint.objects.create(
            tenant=request.tenant,
            url=data['url'],
            events=data['events'],
            secret=WebhookEndpoint.generate_secret(),
        )
        return Response(
            {'status': 'ok', 'data': WebhookEndpointSerializer(endpoint).data},
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=['Webhooks'])
class WebhookEndpointDetailView(APIView):
    """DELETE /api/v1/webhooks/{id}/ — удалить. POST /test/ — тестовый запрос."""

    permission_classes = [IsAuthenticated]

    def _get(self, pk, tenant):
        """Возвращает вебхук тенанта или None."""
        try:
            return WebhookEndpoint.objects.get(pk=pk, tenant=tenant)
        except WebhookEndpoint.DoesNotExist:
            return None

    def delete(self, request, pk):
        """Удаляет вебхук-эндпоинт."""
        endpoint = self._get(pk, request.tenant)
        if endpoint is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        endpoint.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Webhooks'])
class WebhookEndpointTestView(APIView):
    """POST /api/v1/webhooks/{id}/test/ — отправить тестовый payload на URL вебхука."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        """Отправляет тестовый ping-запрос на зарегистрированный URL вебхука."""
        try:
            endpoint = WebhookEndpoint.objects.get(pk=pk, tenant=request.tenant)
        except WebhookEndpoint.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        payload = json.dumps({
            'event': 'test.ping',
            'tenant': request.tenant.slug,
            'data': {'message': 'MAP webhook test'},
        })
        signature = hmac.new(
            endpoint.secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        try:
            resp = requests.post(
                endpoint.url,
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-MAP-Signature': f'sha256={signature}',
                    'X-MAP-Event': 'test.ping',
                },
                timeout=10,
            )
            return Response({
                'status': 'ok',
                'data': {'http_status': resp.status_code, 'ok': resp.status_code < 400},
            })
        except requests.RequestException as exc:
            return Response(
                {'status': 'error', 'detail': str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


@extend_schema(tags=['Webhooks'])
class WebhookEventsView(APIView):
    """GET /api/v1/webhooks/events/ — список доступных типов событий."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Возвращает все поддерживаемые типы событий для вебхуков."""
        return Response({'status': 'ok', 'data': WEBHOOK_EVENTS})


@extend_schema(tags=['Auth'])
class MeView(APIView):
    """
    GET /api/v1/auth/me/ — информация о текущем пользователе и тенанте.

    Используется фронтендом для восстановления auth-состояния после refresh.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.tenants.models import TenantUser

        user = request.user
        tenant = request.tenant

        # Получаем role пользователя в текущем тенанте
        role = None
        if tenant:
            membership = TenantUser.objects.filter(user=user, tenant=tenant).first()
            role = membership.role if membership else None

        # Subscription info
        subscription_data = None
        if tenant:
            try:
                sub = tenant.subscription
                from django.utils import timezone
                from apps.billing.models import Subscription as Sub
                effective_status = sub.status
                if (
                    sub.status == Sub.STATUS_TRIAL
                    and sub.current_period_end
                    and sub.current_period_end < timezone.now().date()
                ):
                    effective_status = Sub.STATUS_PAST_DUE
                subscription_data = {
                    'plan_slug': sub.plan.slug,
                    'plan_name': sub.plan.name,
                    'status': effective_status,
                    'current_period_end': sub.current_period_end.isoformat() if sub.current_period_end else None,
                }
            except Exception:
                pass

        return Response({
            'status': 'ok',
            'data': {
                'user': {
                    'id': user.pk,
                    'email': user.email,
                    'phone': getattr(user, 'phone', ''),
                },
                'tenant': {
                    'id': tenant.pk,
                    'slug': tenant.slug,
                    'name': tenant.name,
                } if tenant else None,
                'role': role,
                'subscription': subscription_data,
            },
        })
