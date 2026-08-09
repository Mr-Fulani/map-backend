from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.users.serializers import (
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    EmailConfirmationSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ProfileUpdateSerializer,
)
from apps.users.services import ProfileService
from apps.users.throttles import CredentialScopedRateThrottle
from apps.tenants.permissions import HumanUserOnly


_PROFILE_UPDATE_RESPONSE = inline_serializer(
    name='ProfileUpdateResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='ProfileUpdateData',
            fields={'phone': serializers.CharField(allow_blank=True)},
        ),
    },
)
_PROFILE_STATUS_RESPONSE = inline_serializer(
    name='ProfileStatusResponse',
    fields={'status': serializers.CharField()},
)
_PROFILE_MESSAGE_RESPONSE = inline_serializer(
    name='ProfileMessageResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='ProfileMessageData',
            fields={'message': serializers.CharField()},
        ),
    },
)


@extend_schema(tags=['Profile'])
class UpdateProfileView(APIView):
    """PATCH /api/v1/auth/profile/ — обновить телефон текущего пользователя."""

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(
        request=ProfileUpdateSerializer,
        responses=_PROFILE_UPDATE_RESPONSE,
    )
    def patch(self, request):
        """Принимает phone, сохраняет и возвращает обновлённые данные."""
        serializer = ProfileUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ProfileService.update_phone(request.user, serializer.validated_data.get('phone', ''))
        return Response({
            'status': 'ok',
            'data': {'phone': request.user.phone},
        })


@extend_schema(tags=['Profile'])
class ChangePasswordView(APIView):
    """POST /api/v1/auth/change-password/ — сменить пароль."""

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(
        request=ChangePasswordSerializer,
        responses=_PROFILE_STATUS_RESPONSE,
    )
    def post(self, request):
        """Проверяет текущий пароль и устанавливает новый."""
        serializer = ChangePasswordSerializer(data=request.data, context={'user': request.user})
        serializer.is_valid(raise_exception=True)
        try:
            ProfileService.change_password(
                request.user,
                serializer.validated_data['current_password'],
                serializer.validated_data['new_password'],
            )
        except ValueError as exc:
            return Response(
                {'status': 'error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok'})


@extend_schema(tags=['Profile'])
class ChangeEmailRequestView(APIView):
    """POST /api/v1/auth/change-email/ — запросить смену email (шлёт письмо на новый адрес)."""

    permission_classes = [IsAuthenticated, HumanUserOnly]

    @extend_schema(
        request=ChangeEmailSerializer,
        responses=_PROFILE_MESSAGE_RESPONSE,
    )
    def post(self, request):
        """Отправляет письмо с подтверждением на новый email."""
        serializer = ChangeEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            ProfileService.request_email_change(
                request.user,
                serializer.validated_data['new_email'],
                serializer.validated_data['current_password'],
            )
        except ValueError as exc:
            return Response(
                {'status': 'error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            'status': 'ok',
            'data': {'message': 'Письмо с подтверждением отправлено на новый email'},
        })


@extend_schema(tags=['Profile'])
class ConfirmEmailView(APIView):
    """POST /api/v1/auth/confirm-email/ — подтвердить смену email одноразовым токеном."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        request=EmailConfirmationSerializer,
        responses=_PROFILE_MESSAGE_RESPONSE,
    )
    def post(self, request):
        """Верифицирует токен и обновляет email пользователя."""
        serializer = EmailConfirmationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            ProfileService.confirm_email_change(serializer.validated_data['token'])
        except ValueError as exc:
            return Response(
                {'status': 'error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': {'message': 'Email успешно изменён'}})


@extend_schema(tags=['Auth'])
class PasswordResetRequestView(APIView):
    """POST /auth/password-reset/ — единообразно принимает любой корректный email."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle, CredentialScopedRateThrottle]
    throttle_scope = 'password_reset_request'

    @extend_schema(request=PasswordResetRequestSerializer, responses={202: _PROFILE_MESSAGE_RESPONSE})
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ProfileService.request_password_reset(serializer.validated_data['email'])
        return Response({
            'status': 'ok',
            'data': {'message': 'Если аккаунт существует, письмо уже отправлено.'},
        }, status=status.HTTP_202_ACCEPTED)


@extend_schema(tags=['Auth'])
class PasswordResetConfirmView(APIView):
    """POST /auth/password-reset/confirm/ — установить новый пароль."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'password_reset_confirm'

    @extend_schema(request=PasswordResetConfirmSerializer, responses=_PROFILE_STATUS_RESPONSE)
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            ProfileService.confirm_password_reset(**serializer.validated_data)
        except ValueError as exc:
            return Response(
                {'status': 'error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok'})
