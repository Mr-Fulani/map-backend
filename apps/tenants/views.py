import hmac
import hashlib
import json

import requests
from django.db import transaction
from django.utils.timezone import now
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.url_security import (
    REDIRECT_NONE,
    ResponseTooLarge,
    UnsafePublicURL,
    request_public_http_url,
)
from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
)
from apps.tenants.models import (
    APIKey, CatalogDomain, TenantCatalogDomain, WEBHOOK_EVENTS, WebhookEndpoint,
)
from apps.tenants.permissions import (
    HumanUserOnly,
    TenantAdminPermission,
    TenantAdminWritePermission,
)
from apps.tenants.serializers import (
    APIKeyCreateSerializer,
    APIKeyCreatedSerializer,
    APIKeySerializer,
    CatalogDomainSelectionSerializer,
    CatalogDomainSerializer,
    RegisterSerializer,
    TenantSerializer,
    TenantUserSerializer,
    WebhookDeliverySerializer,
    WebhookEndpointCreatedSerializer,
    WebhookEndpointSerializer,
    WebhookEndpointWriteSerializer,
)
from apps.tenants.services import (
    APIKeyService,
    DuplicateWebhookEndpoint,
    TenantService,
    WebhookEndpointQuotaExceeded,
    WebhookEndpointService,
)
from apps.users.throttles import CredentialScopedRateThrottle


def _success_response(name, data=None):
    fields: dict[str, serializers.Field] = {
        'status': serializers.CharField(),
    }
    if data is not None:
        fields['data'] = data
    return inline_serializer(name=name, fields=fields)


_me_data = inline_serializer(
    name='CurrentTenantContext',
    fields={
        'user': inline_serializer(
            name='CurrentUserSummary',
            fields={
                'id': serializers.IntegerField(),
                'email': serializers.EmailField(),
                'phone': serializers.CharField(allow_blank=True),
            },
        ),
        'tenant': inline_serializer(
            name='CurrentTenantSummary',
            fields={
                'id': serializers.IntegerField(),
                'slug': serializers.SlugField(),
                'name': serializers.CharField(),
            },
            allow_null=True,
        ),
        'role': serializers.CharField(allow_null=True),
        'subscription': inline_serializer(
            name='CurrentSubscriptionSummary',
            fields={
                'plan_slug': serializers.SlugField(),
                'plan_name': serializers.CharField(),
                'status': serializers.CharField(),
                'access_mode': serializers.CharField(),
                'current_period_end': serializers.DateTimeField(allow_null=True),
            },
            allow_null=True,
        ),
    },
)

_webhook_endpoint_conflict = inline_serializer(
    name='WebhookEndpointConflictResponse',
    fields={
        'status': serializers.CharField(),
        'code': serializers.CharField(),
        'message': serializers.CharField(),
    },
)


@extend_schema(tags=['Auth'])
class RegisterView(APIView):
    """POST /api/v1/auth/register/ — создать тенанта и получить API Key."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle, CredentialScopedRateThrottle]
    throttle_scope = 'auth_register'

    @extend_schema(
        request=RegisterSerializer,
        responses={
            201: _success_response(
                'RegisterResponse',
                inline_serializer(
                    name='RegisterResult',
                    fields={
                        'tenant': TenantSerializer(),
                        'api_key': serializers.CharField(),
                        'warning': serializers.CharField(),
                    },
                ),
            ),
        },
    )
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
                'warning': (
                    'Временный read-only API Key действует 24 часа. '
                    'Создайте scoped integration key в настройках организации.'
                ),
            },
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Tenant'])
class TenantDetailView(APIView):
    """GET /api/v1/tenant/ — информация о текущем тенанте."""

    permission_classes = [IsAuthenticated]
    api_key_enabled = True
    api_key_scopes = {
        'GET': {'tenant:read'},
        'HEAD': {'tenant:read'},
        'OPTIONS': {'tenant:read'},
    }

    @extend_schema(responses={200: _success_response('TenantDetailResponse', TenantSerializer())})
    def get(self, request):
        return Response({
            'status': 'ok',
            'data': TenantSerializer(request.tenant).data,
        })


@extend_schema(tags=['Tenant'])
class CatalogDomainListView(APIView):
    """GET/POST /api/v1/catalog-domains/ — platform-домены для dashboard."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(responses={
        200: _success_response('CatalogDomainListResponse', CatalogDomainSerializer(many=True)),
    })
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

    @extend_schema(
        request=inline_serializer(
            name='CatalogDomainToggleRequest',
            fields={
                'domain_slug': serializers.SlugField(),
                'is_enabled': serializers.BooleanField(),
            },
        ),
        responses={
            200: _success_response(
                'CatalogDomainToggleResponse',
                inline_serializer(
                    name='CatalogDomainToggleResult',
                    fields={
                        'domain_slug': serializers.SlugField(),
                        'is_enabled': serializers.BooleanField(),
                    },
                ),
            ),
        },
    )
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
            ProductCategorySeedService.seed_tenant_primary_categories(request.tenant, domain)

        return Response({
            'status': 'ok',
            'data': {
                'domain_slug': domain.slug,
                'is_enabled': enabling.is_enabled,
            },
        })

    @extend_schema(
        request=CatalogDomainSelectionSerializer,
        responses={
            200: _success_response(
                'CatalogDomainSelectionResponse',
                CatalogDomainSerializer(many=True),
            ),
        },
    )
    def put(self, request):
        serializer = CatalogDomainSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        selected_slug_list = serializer.validated_data['enabled_domain_slugs']
        selected_slugs = set(selected_slug_list)
        domains = list(
            CatalogDomain.objects.filter(is_active=True).exclude(
                slug__in=['mixed', 'unknown'],
            ).order_by('sort_order', 'name')
        )
        domains_by_slug = {domain.slug: domain for domain in domains}
        unknown_slugs = sorted(selected_slugs - domains_by_slug.keys())
        if unknown_slugs:
            return Response(
                {
                    'status': 'error',
                    'code': 'validation_error',
                    'message': 'Неизвестные направления каталога: ' + ', '.join(unknown_slugs),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        previously_enabled = set(
            TenantCatalogDomain.objects.filter(
                tenant=request.tenant,
                is_enabled=True,
            ).values_list('domain__slug', flat=True)
        )
        selected_domains = [domains_by_slug[slug] for slug in selected_slug_list]
        with transaction.atomic():
            TenantCatalogDomain.objects.filter(tenant=request.tenant).update(
                is_enabled=False,
                updated_at=now(),
            )
            for domain in selected_domains:
                TenantCatalogDomain.objects.update_or_create(
                    tenant=request.tenant,
                    domain=domain,
                    defaults={'is_enabled': True},
                )
            if selected_slugs - previously_enabled:
                from apps.products.services import ProductCategorySeedService
                for domain in selected_domains:
                    if domain.slug not in previously_enabled:
                        ProductCategorySeedService.seed_tenant_primary_categories(
                            request.tenant,
                            domain,
                        )

        enabled_ids = {domain.pk for domain in selected_domains}
        return Response({
            'status': 'ok',
            'data': CatalogDomainSerializer(
                domains,
                many=True,
                context={
                    'tenant': request.tenant,
                    'enabled_domain_ids': enabled_ids,
                },
            ).data,
        })


@extend_schema(tags=['Tenant'])
class TenantUserListView(APIView):
    """GET /api/v1/tenant/users/ — список пользователей тенанта."""

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(responses={
        200: _success_response('TenantUserListResponse', TenantUserSerializer(many=True)),
    })
    def get(self, request):
        members = request.tenant.members.select_related('user').all()
        return Response({
            'status': 'ok',
            'data': TenantUserSerializer(members, many=True).data,
        })


@extend_schema(tags=['API Keys'])
class APIKeyListView(APIView):
    """GET /api/v1/tenant/api-keys/ — список API ключей. POST — создать новый."""

    permission_classes = [IsAuthenticated, HumanUserOnly, TenantAdminPermission]

    @extend_schema(responses={
        200: _success_response('APIKeyListResponse', APIKeySerializer(many=True)),
    })
    def get(self, request):
        keys = APIKey.objects.filter(tenant=request.tenant)
        return Response({
            'status': 'ok',
            'data': APIKeySerializer(keys, many=True).data,
        })

    @extend_schema(
        request=APIKeyCreateSerializer,
        responses={
            201: _success_response('APIKeyCreateResponse', APIKeyCreatedSerializer()),
        },
    )
    def post(self, request):
        serializer = APIKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        api_key, plaintext = APIKeyService.create_key(
            tenant=request.tenant,
            name=serializer.validated_data['name'],
            role=serializer.validated_data['role'],
            scopes=serializer.validated_data['scopes'],
            expires_at=serializer.validated_data['expires_at'],
            created_by=request.user,
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

    permission_classes = [IsAuthenticated, HumanUserOnly, TenantAdminPermission]

    @extend_schema(
        request=None,
        responses={200: _success_response('APIKeyRevokeResponse')},
    )
    def delete(self, request, key_id):
        APIKeyService.revoke_key(
            key_id=key_id,
            tenant=request.tenant,
            revoked_by=request.user,
        )
        return Response({'status': 'ok'}, status=status.HTTP_200_OK)


@extend_schema(tags=['Webhooks'])
class WebhookEndpointListView(APIView):
    """GET /api/v1/webhooks/ — список вебхуков. POST — создать."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'webhook_create_principal'
    tenant_throttle_scope = 'webhook_create_tenant'

    @extend_schema(responses={
        200: _success_response(
            'WebhookEndpointListResponse',
            WebhookEndpointSerializer(many=True),
        ),
    })
    def get(self, request):
        """Возвращает вебхук-эндпоинты текущего тенанта."""
        qs = WebhookEndpoint.objects.filter(tenant=request.tenant).order_by('-created_at')
        return Response({'status': 'ok', 'data': WebhookEndpointSerializer(qs, many=True).data})

    @extend_schema(
        request=WebhookEndpointWriteSerializer,
        responses={
            201: _success_response(
                'WebhookEndpointCreateResponse',
                WebhookEndpointCreatedSerializer(),
            ),
            409: _webhook_endpoint_conflict,
        },
    )
    def post(self, request):
        """Создаёт новый вебхук-эндпоинт с автоматически сгенерированным секретом."""
        serializer = WebhookEndpointWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            endpoint, plaintext_secret = WebhookEndpointService.create_endpoint(
                tenant=request.tenant,
                url=data['url'],
                events=data['events'],
            )
        except DuplicateWebhookEndpoint as exc:
            return Response(
                {
                    'status': 'error',
                    'code': 'duplicate_webhook_endpoint',
                    'message': str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except WebhookEndpointQuotaExceeded as exc:
            return Response(
                {
                    'status': 'error',
                    'code': 'webhook_endpoint_quota_exceeded',
                    'message': str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {
                'status': 'ok',
                'data': {
                    **WebhookEndpointSerializer(endpoint).data,
                    'secret': plaintext_secret,
                    'warning': 'Сохраните secret — он больше не будет показан.',
                },
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=['Webhooks'])
class WebhookEndpointDetailView(APIView):
    """DELETE /api/v1/webhooks/{id}/ — удалить. POST /test/ — тестовый запрос."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    def _get(self, pk, tenant):
        """Возвращает вебхук тенанта или None."""
        try:
            return WebhookEndpoint.objects.get(pk=pk, tenant=tenant)
        except WebhookEndpoint.DoesNotExist:
            return None

    @extend_schema(request=None, responses={204: None})
    def delete(self, request, pk):
        """Удаляет вебхук-эндпоинт."""
        endpoint = self._get(pk, request.tenant)
        if endpoint is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        endpoint.soft_delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Webhooks'])
class WebhookEndpointTestView(APIView):
    """POST /api/v1/webhooks/{id}/test/ — отправить тестовый payload на URL вебхука."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'webhook_test_principal'
    tenant_throttle_scope = 'webhook_test_tenant'

    @extend_schema(
        request=None,
        responses={
            200: _success_response(
                'WebhookTestResponse',
                inline_serializer(
                    name='WebhookTestResult',
                    fields={
                        'http_status': serializers.IntegerField(),
                        'ok': serializers.BooleanField(),
                    },
                ),
            ),
        },
    )
    def post(self, request, pk):
        """Отправляет тестовый ping-запрос на зарегистрированный URL вебхука."""
        try:
            endpoint = WebhookEndpoint.objects.get(
                pk=pk,
                tenant=request.tenant,
                is_active=True,
            )
        except WebhookEndpoint.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        payload = json.dumps({
            'event': 'test.ping',
            'tenant': request.tenant.slug,
            'data': {'message': 'MAP webhook test'},
        })
        signature = hmac.new(
            endpoint.get_secret().encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        try:
            resp = request_public_http_url(
                endpoint.url,
                method='POST',
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-MAP-Signature': f'sha256={signature}',
                    'X-MAP-Event': 'test.ping',
                },
                timeout=(5, 10),
                status_only=True,
                redirect_policy=REDIRECT_NONE,
            )
            return Response({
                'status': 'ok',
                'data': {'http_status': resp.status_code, 'ok': resp.status_code < 400},
            })
        except (UnsafePublicURL, ResponseTooLarge) as exc:
            return Response(
                {'status': 'error', 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except requests.RequestException as exc:
            return Response(
                {'status': 'error', 'detail': str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


@extend_schema(tags=['Webhooks'])
class WebhookEventsView(APIView):
    """GET /api/v1/webhooks/events/ — список доступных типов событий."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(responses={
        200: _success_response(
            'WebhookEventListResponse',
            serializers.ListField(child=serializers.CharField()),
        ),
    })
    def get(self, request):
        """Возвращает все поддерживаемые типы событий для вебхуков."""
        return Response({'status': 'ok', 'data': WEBHOOK_EVENTS})


@extend_schema(tags=['Webhooks'])
class WebhookDeliveryListView(APIView):
    """GET /api/v1/webhooks/deliveries/ — аудит исходящих доставок."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(
        parameters=[
            OpenApiParameter('status', str, description='Delivery status'),
            OpenApiParameter('endpoint_id', int, description='Webhook endpoint ID'),
        ],
        responses={
            200: _success_response(
                'WebhookDeliveryListResponse',
                WebhookDeliverySerializer(many=True),
            ),
        },
    )
    def get(self, request):
        from apps.tenants.models import WebhookDelivery

        qs = WebhookDelivery.objects.filter(
            event__tenant=request.tenant,
        ).select_related('event').order_by('-created_at')
        delivery_status = request.query_params.get('status')
        if delivery_status:
            qs = qs.filter(status=delivery_status)
        endpoint_id = request.query_params.get('endpoint_id')
        if endpoint_id:
            qs = qs.filter(endpoint_id=endpoint_id)
        return Response({
            'status': 'ok',
            'data': WebhookDeliverySerializer(qs[:200], many=True).data,
        })


@extend_schema(tags=['Auth'])
class MeView(APIView):
    """
    GET /api/v1/auth/me/ — информация о текущем пользователе и тенанте.

    Используется фронтендом для восстановления auth-состояния после refresh.
    """

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(responses={200: _success_response('MeResponse', _me_data)})
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
            from apps.billing.models import Subscription

            try:
                sub = tenant.subscription
                subscription_data = {
                    'plan_slug': sub.plan.slug,
                    'plan_name': sub.plan.name,
                    'status': sub.effective_status,
                    'access_mode': sub.access_mode,
                    'current_period_end': sub.current_period_end.isoformat() if sub.current_period_end else None,
                }
            except Subscription.DoesNotExist:
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
