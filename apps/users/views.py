from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.serializers import (
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    ProfileUpdateSerializer,
)
from apps.users.services import ProfileService


@extend_schema(tags=['Profile'])
class UpdateProfileView(APIView):
    """PATCH /api/v1/auth/profile/ — обновить телефон текущего пользователя."""

    permission_classes = [IsAuthenticated]

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

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Проверяет текущий пароль и устанавливает новый."""
        serializer = ChangePasswordSerializer(data=request.data)
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

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Отправляет письмо с подтверждением на новый email."""
        serializer = ChangeEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            ProfileService.request_email_change(
                request.user,
                serializer.validated_data['new_email'],
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
    """GET /api/v1/auth/confirm-email/?token=... — подтвердить смену email по токену."""

    permission_classes = [AllowAny]

    def get(self, request):
        """Верифицирует токен и обновляет email пользователя."""
        token = request.query_params.get('token', '')
        try:
            ProfileService.confirm_email_change(token)
        except ValueError as exc:
            return Response(
                {'status': 'error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': {'message': 'Email успешно изменён'}})
