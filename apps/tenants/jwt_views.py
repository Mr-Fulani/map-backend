"""
JWT views для Dashboard авторизации.
"""

from rest_framework_simplejwt.views import TokenObtainPairView

from apps.tenants.jwt_serializers import TenantTokenObtainPairSerializer


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
