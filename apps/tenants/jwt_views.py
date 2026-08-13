"""JWT views for header-based clients and CSRF-protected browser sessions."""

from datetime import timedelta
from typing import Any, Protocol, cast

from django.conf import settings
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.tenants.jwt_serializers import (
    BrowserCSRFFailureSerializer,
    BrowserCSRFResponseSerializer,
    BrowserSessionRejectedSerializer,
    BrowserTokenObtainPairResponseSerializer,
    BrowserTokenRefreshResponseSerializer,
    LogoutSerializer,
    TenantTokenObtainPairResponseSerializer, TenantTokenObtainPairSerializer,
    TenantTokenRefreshSerializer,
)
from apps.tenants.permissions import HumanUserOnly
from apps.tenants.session_tokens import (
    blacklist_refresh_token,
    revoke_all_sessions_from_refresh,
    revoke_all_user_sessions,
)
from apps.users.throttles import CredentialScopedRateThrottle


class _ResponseFinalizer(Protocol):
    def finalize_response(
        self,
        request: Any,
        response: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...


def _disable_auth_response_caching(response):
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


def _set_refresh_cookie(response, refresh_token):
    refresh_lifetime = cast(
        timedelta,
        settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
    )
    response.set_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=int(refresh_lifetime.total_seconds()),
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        secure=not settings.DEBUG,
        httponly=True,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


def _delete_refresh_cookie(response):
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


def _browser_session_rejected_response():
    """Return an authentication rejection without conflating it with CSRF."""
    response = Response(
        {
            'status': 'error',
            'code': 'unauthorized',
            'message': 'Сессия истекла. Войдите снова.',
        },
        status=status.HTTP_401_UNAUTHORIZED,
    )
    _delete_refresh_cookie(response)
    return response


class NoStoreAuthResponseMixin:
    """Prevent credentials and token-bearing responses from browser/proxy caches."""

    def finalize_response(self, request, response, *args, **kwargs):
        finalizer = cast(_ResponseFinalizer, super())
        response = finalizer.finalize_response(request, response, *args, **kwargs)
        return _disable_auth_response_caching(response)


@extend_schema_view(
    post=extend_schema(
        tags=['Auth'],
        summary='Войти по email и паролю',
        request=TenantTokenObtainPairSerializer,
        responses={200: TenantTokenObtainPairResponseSerializer},
    ),
)
class TenantTokenObtainPairView(NoStoreAuthResponseMixin, TokenObtainPairView):
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
    throttle_classes = [ScopedRateThrottle, CredentialScopedRateThrottle]
    throttle_scope = 'auth_login'


@extend_schema_view(
    post=extend_schema(
        tags=['Auth'],
        summary='Обновить JWT access-токен',
    ),
)
class TenantTokenRefreshView(NoStoreAuthResponseMixin, TokenRefreshView):
    """Refresh JWT с повторной проверкой tenant membership."""

    serializer_class = TenantTokenRefreshSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_refresh'


@extend_schema(tags=['Auth'])
class LogoutView(NoStoreAuthResponseMixin, APIView):
    """Revoke one refresh token for a header-based/CLI client."""

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(request=LogoutSerializer, responses={204: None})
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        blacklist_refresh_token(
            serializer.validated_data['refresh'],
            expected_user_id=request.user.pk,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Auth'])
class LogoutAllView(NoStoreAuthResponseMixin, APIView):
    """Revoke all sessions, including the access token used for this request."""

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        revoke_all_user_sessions(request.user.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
@extend_schema(tags=['Browser Auth'])
class BrowserCSRFView(NoStoreAuthResponseMixin, APIView):
    """Issue a readable masked CSRF token while the cookie remains HttpOnly."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(responses={200: BrowserCSRFResponseSerializer})
    def get(self, request):
        return Response({'status': 'ok', 'csrf_token': get_token(request)})


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
@extend_schema(tags=['Browser Auth'])
class BrowserLoginView(NoStoreAuthResponseMixin, APIView):
    """Login with refresh in HttpOnly cookie and access token in the response."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle, CredentialScopedRateThrottle]
    throttle_scope = 'auth_login'

    @extend_schema(
        request=TenantTokenObtainPairSerializer,
        responses={200: BrowserTokenObtainPairResponseSerializer},
    )
    def post(self, request):
        serializer = TenantTokenObtainPairSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        refresh = payload.pop('refresh')
        response = Response(payload)
        _set_refresh_cookie(response, refresh)
        return response


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
@extend_schema(tags=['Browser Auth'])
class BrowserRefreshView(NoStoreAuthResponseMixin, APIView):
    """Atomically rotate the HttpOnly refresh cookie."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_refresh'

    @extend_schema(
        request=None,
        responses={
            200: BrowserTokenRefreshResponseSerializer,
            401: BrowserSessionRejectedSerializer,
            403: BrowserCSRFFailureSerializer,
        },
    )
    def post(self, request):
        raw_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME, '')
        if not raw_token:
            return _browser_session_rejected_response()
        serializer = TenantTokenRefreshSerializer(data={
            'refresh': raw_token,
        })
        try:
            serializer.is_valid(raise_exception=True)
        except (InvalidToken, ValidationError):
            return _browser_session_rejected_response()
        payload = dict(serializer.validated_data)
        refresh = payload.pop('refresh')
        response = Response(payload)
        _set_refresh_cookie(response, refresh)
        return response


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
@extend_schema(tags=['Browser Auth'])
class BrowserLogoutView(NoStoreAuthResponseMixin, APIView):
    """Revoke the browser's current refresh token and clear its cookie."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        raw_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME, '')
        if raw_token:
            try:
                blacklist_refresh_token(raw_token)
            except InvalidToken:
                # Logout остаётся идемпотентным и всегда очищает локальный cookie.
                pass
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _delete_refresh_cookie(response)
        return response


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
@extend_schema(tags=['Browser Auth'])
class BrowserLogoutAllView(NoStoreAuthResponseMixin, APIView):
    """Use the browser refresh credential to revoke every user session."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        raw_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME, '')
        revoke_all_sessions_from_refresh(raw_token)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _delete_refresh_cookie(response)
        return response
