"""
JWT views для Dashboard авторизации.
"""

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.tenants.jwt_serializers import (
    TenantTokenObtainPairResponseSerializer, TenantTokenObtainPairSerializer,
    TenantTokenRefreshSerializer,
)


@extend_schema_view(
    post=extend_schema(
        tags=['Auth'],
        summary='Войти по email и паролю',
        request=TenantTokenObtainPairSerializer,
        responses={200: TenantTokenObtainPairResponseSerializer},
    ),
)
class TenantTokenObtainPairView(TokenObtainPairView):
    """
    POST /api/v1/auth/token/

    Login по email + password. Возвращает access и refresh токены,
    а также информацию о тенанте и роли пользователя.

    Request body:
        {
            "email": "user@example.com",
            "password": "password123",
            "tenant_slug": "my-company"  // optional
        }

    Response:
        {
            "access": "eyJ...",
            "refresh": "eyJ...",
            "tenant": {"id": 1, "slug": "my-company", "name": "My Company"},
            "role": "owner",
            "user": {"id": 1, "email": "user@example.com"}
        }
    """
    serializer_class = TenantTokenObtainPairSerializer


@extend_schema_view(
    post=extend_schema(
        tags=['Auth'],
        summary='Обновить JWT access-токен',
    ),
)
class TenantTokenRefreshView(TokenRefreshView):
    """Refresh JWT с повторной проверкой tenant membership."""

    serializer_class = TenantTokenRefreshSerializer
