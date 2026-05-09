from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import APIKey
from apps.tenants.serializers import (
    APIKeyCreateSerializer,
    APIKeySerializer,
    RegisterSerializer,
    TenantSerializer,
    TenantUserSerializer,
)
from apps.tenants.services import APIKeyService, TenantService


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


class TenantDetailView(APIView):
    """GET /api/v1/tenant/ — информация о текущем тенанте."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'status': 'ok',
            'data': TenantSerializer(request.tenant).data,
        })


class TenantUserListView(APIView):
    """GET /api/v1/tenant/users/ — список пользователей тенанта."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        members = request.tenant.members.select_related('user').all()
        return Response({
            'status': 'ok',
            'data': TenantUserSerializer(members, many=True).data,
        })


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


class APIKeyRevokeView(APIView):
    """DELETE /api/v1/tenant/api-keys/{id}/ — отозвать ключ."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, key_id):
        APIKeyService.revoke_key(key_id=key_id, tenant=request.tenant)
        return Response({'status': 'ok'}, status=status.HTTP_200_OK)


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
                subscription_data = {
                    'plan_slug': sub.plan.slug,
                    'plan_name': sub.plan.name,
                    'status': sub.status,
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

