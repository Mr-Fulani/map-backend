import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.browser_sessions.models import BrowserSession
from apps.browser_sessions.serializers import (
    BrowserLoginStartSerializer,
    BrowserSessionStatusSerializer,
    BrowserVerifySerializer,
    PublishMethodSerializer,
)
from apps.browser_sessions.session_manager import SessionManager
from apps.browser_sessions.services import BrowserSessionService
from apps.marketplaces.models import MarketplaceAccount

logger = logging.getLogger(__name__)


def _get_account(pk, tenant) -> MarketplaceAccount | None:
    try:
        return MarketplaceAccount.objects.get(pk=pk, tenant=tenant)
    except MarketplaceAccount.DoesNotExist:
        return None


@extend_schema(tags=['Browser Sessions'])
class PublishMethodView(APIView):
    """
    PATCH /api/v1/accounts/{id}/publish-method/
    Переключает метод публикации: autoload ↔ web.
    При переходе в web-режим требует login + password от Avito.
    """

    def patch(self, request, pk):
        """Переключает метод публикации аккаунта."""
        account = _get_account(pk, request.tenant)
        if not account:
            return Response({'detail': 'Аккаунт не найден'}, status=status.HTTP_404_NOT_FOUND)

        serializer = PublishMethodSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data['publish_method'] == MarketplaceAccount.PUBLISH_WEB:
            BrowserSessionService.initialize(
                account, data['login'], data['password'],
            )
            return Response({'status': 'started', 'detail': 'Логин запущен в фоне'})

        BrowserSessionService.switch_to_autoload(account)
        return Response({'status': 'switched', 'detail': 'Аккаунт переключён на Autoload'})


@extend_schema(tags=['Browser Sessions'])
class BrowserLoginStatusView(APIView):
    """
    GET /api/v1/accounts/{id}/browser-login/
    Возвращает текущий статус фонового логина.
    Фронт polling'ует этот endpoint пока status != 'success' | 'error'.
    """

    def get(self, request, pk):
        """Возвращает статус логина и доступные методы верификации."""
        account = _get_account(pk, request.tenant)
        if not account:
            return Response({'detail': 'Аккаунт не найден'}, status=status.HTTP_404_NOT_FOUND)

        login_status = SessionManager.get_login_status(account.pk)

        # Добавляем данные BrowserSession если есть
        try:
            session = account.browser_session
            login_status['session'] = BrowserSessionStatusSerializer(session).data
        except BrowserSession.DoesNotExist:
            login_status['session'] = None

        return Response(login_status)


@extend_schema(tags=['Browser Sessions'])
class BrowserVerifyView(APIView):
    """
    POST /api/v1/accounts/{id}/browser-verify/
    Тенант передаёт SMS или email-код для завершения логина.
    Тело: {"code": "12345", "method": "sms"}
    """

    def post(self, request, pk):
        """Передаёт код подтверждения ожидающей Celery-задаче."""
        account = _get_account(pk, request.tenant)
        if not account:
            return Response({'detail': 'Аккаунт не найден'}, status=status.HTTP_404_NOT_FOUND)

        serializer = BrowserVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        current = SessionManager.get_login_status(account.pk)
        if current['status'] != 'awaiting_code':
            return Response(
                {'detail': f'Ожидание кода не активно (текущий статус: {current["status"]})'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        SessionManager.submit_code_to_redis(account.pk, serializer.validated_data['code'])
        return Response({'status': 'submitted', 'detail': 'Код передан, ожидайте завершения входа'})


@extend_schema(tags=['Browser Sessions'])
class BrowserSessionView(APIView):
    """
    GET  /api/v1/accounts/{id}/browser-session/ — состояние сессии
    DELETE /api/v1/accounts/{id}/browser-session/ — сбросить сессию
    """

    def get(self, request, pk):
        """Возвращает состояние BrowserSession аккаунта."""
        account = _get_account(pk, request.tenant)
        if not account:
            return Response({'detail': 'Аккаунт не найден'}, status=status.HTTP_404_NOT_FOUND)

        try:
            session = account.browser_session
        except BrowserSession.DoesNotExist:
            return Response({'detail': 'Web-режим не активирован'}, status=status.HTTP_404_NOT_FOUND)

        return Response(BrowserSessionStatusSerializer(session).data)

    def delete(self, request, pk):
        """Сбрасывает сессию — тенанту нужно будет войти заново."""
        account = _get_account(pk, request.tenant)
        if not account:
            return Response({'detail': 'Аккаунт не найден'}, status=status.HTTP_404_NOT_FOUND)

        try:
            session = account.browser_session
        except BrowserSession.DoesNotExist:
            return Response({'detail': 'Web-режим не активирован'}, status=status.HTTP_404_NOT_FOUND)

        from apps.browser_sessions.session_manager import session_manager
        session_manager.invalidate(session)
        return Response({'status': 'invalidated'})
