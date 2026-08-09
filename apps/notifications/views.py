import logging
import secrets

from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notifications.models import (
    CONNECT_TOKEN_CONSUMED,
    CONNECT_TOKEN_EXPIRED,
    TenantNotificationSettings,
)
from apps.notifications.telegram import TelegramNotifier
from apps.tenants.permissions import TenantAdminPermission, TenantAdminWritePermission

logger = logging.getLogger(__name__)


class TelegramUpdateValidationError(ValueError):
    """An authenticated Telegram update has an invalid bounded shape."""


def _validated_telegram_text_message(update) -> tuple[str, str, str] | None:
    """Return normalized text/chat fields, ignore non-text updates, reject bad types."""
    if not isinstance(update, dict):
        raise TelegramUpdateValidationError('Telegram update должен быть объектом.')
    update_id = update.get('update_id')
    if (
        not isinstance(update_id, int)
        or isinstance(update_id, bool)
        or update_id < 0
    ):
        raise TelegramUpdateValidationError('Некорректный update_id.')

    message = update.get('message')
    if message is None:
        message = update.get('edited_message')
    if message is None:
        return None
    if not isinstance(message, dict):
        raise TelegramUpdateValidationError('Telegram message должен быть объектом.')

    text = message.get('text')
    if text is None:
        return None
    if not isinstance(text, str) or len(text) > 4096:
        raise TelegramUpdateValidationError('Некорректный текст Telegram message.')

    chat = message.get('chat')
    if not isinstance(chat, dict):
        raise TelegramUpdateValidationError('Telegram chat должен быть объектом.')
    raw_chat_id = chat.get('id')
    if not isinstance(raw_chat_id, int) or isinstance(raw_chat_id, bool):
        raise TelegramUpdateValidationError('Некорректный Telegram chat_id.')
    chat_id = str(raw_chat_id)
    if len(chat_id) > 50:
        raise TelegramUpdateValidationError('Некорректная длина Telegram chat_id.')

    username = chat.get('username') or chat.get('first_name') or ''
    if not isinstance(username, str):
        raise TelegramUpdateValidationError('Некорректное имя Telegram пользователя.')
    return text.strip(), chat_id, username[:100]


_NOTIFICATION_SETTINGS = inline_serializer(
    name='NotificationSettings',
    fields={
        'telegram_connected': serializers.BooleanField(),
        'telegram_username': serializers.CharField(allow_blank=True),
        'notify_email': serializers.EmailField(allow_blank=True),
        'notify_on_error': serializers.BooleanField(),
        'notify_on_critical': serializers.BooleanField(),
    },
)
_NOTIFICATION_SETTINGS_UPDATE = inline_serializer(
    name='NotificationSettingsUpdate',
    fields={
        'notify_email': serializers.EmailField(required=False, allow_blank=True),
        'notify_on_error': serializers.BooleanField(required=False),
        'notify_on_critical': serializers.BooleanField(required=False),
    },
)
_NOTIFICATION_SETTINGS_RESPONSE = inline_serializer(
    name='NotificationSettingsResponse',
    fields={
        'status': serializers.CharField(),
        'data': _NOTIFICATION_SETTINGS,
    },
)
_TELEGRAM_CONNECT_RESPONSE = inline_serializer(
    name='TelegramConnectResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='TelegramConnectData',
            fields={
                'bot_url': serializers.URLField(),
                'expires_in_minutes': serializers.IntegerField(),
            },
        ),
    },
)
_NOTIFICATION_TEST_RESPONSE = inline_serializer(
    name='NotificationTestResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='NotificationTestData',
            fields={'sent': serializers.BooleanField()},
        ),
    },
)


def _get_or_create_settings(tenant, default_email: str = '') -> TenantNotificationSettings:
    """Возвращает настройки уведомлений тенанта, создавая запись при необходимости.

    При первом создании подставляет email владельца как значение по умолчанию.
    """
    ns, _ = TenantNotificationSettings.objects.get_or_create(tenant=tenant)
    if default_email and not ns.notify_email:
        ns.notify_email = default_email
        ns.save(update_fields=['notify_email'])
    return ns


def _serialize(ns: TenantNotificationSettings) -> dict:
    """Сериализует настройки уведомлений в dict для ответа API."""
    return {
        'telegram_connected': bool(ns.telegram_chat_id),
        'telegram_username': ns.telegram_username,
        'notify_email': ns.notify_email,
        'notify_on_error': ns.notify_on_error,
        'notify_on_critical': ns.notify_on_critical,
    }


@extend_schema(tags=['Notifications'])
class NotificationSettingsView(APIView):
    """
    GET/PUT /api/v1/notifications/settings/ — читать и обновлять настройки уведомлений.

    GET возвращает текущие настройки (telegram_connected, email, флаги).
    PUT принимает notify_email, notify_on_error, notify_on_critical.
    """

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(responses=_NOTIFICATION_SETTINGS_RESPONSE)
    def get(self, request):
        """Возвращает текущие настройки уведомлений тенанта."""
        ns = _get_or_create_settings(request.tenant, default_email=request.user.email)
        return Response({'status': 'ok', 'data': _serialize(ns)})

    @extend_schema(
        request=_NOTIFICATION_SETTINGS_UPDATE,
        responses=_NOTIFICATION_SETTINGS_RESPONSE,
    )
    def put(self, request):
        """
        Обновляет email и флаги уведомлений.

        Telegram привязывается отдельно через /telegram/connect/.
        """
        ns = _get_or_create_settings(request.tenant, default_email=request.user.email)
        data = request.data

        if 'notify_email' in data:
            ns.notify_email = data['notify_email']
        if 'notify_on_error' in data:
            ns.notify_on_error = bool(data['notify_on_error'])
        if 'notify_on_critical' in data:
            ns.notify_on_critical = bool(data['notify_on_critical'])

        ns.save(update_fields=['notify_email', 'notify_on_error', 'notify_on_critical'])
        return Response({'status': 'ok', 'data': _serialize(ns)})


@extend_schema(tags=['Notifications'])
class TelegramConnectView(APIView):
    """
    POST /api/v1/notifications/settings/telegram/connect/ — начать привязку Telegram.

    Генерирует одноразовый токен (TTL 15 мин) и возвращает ссылку на бота.
    Тенант переходит по ссылке, нажимает START → бот сохраняет chat_id.
    """

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(request=None, responses=_TELEGRAM_CONNECT_RESPONSE)
    def post(self, request):
        """Генерирует токен и возвращает bot_url для привязки Telegram."""
        bot_username = settings.TELEGRAM_BOT_USERNAME
        if not bot_username or not settings.TELEGRAM_BOT_TOKEN:
            return Response(
                {'status': 'error', 'detail': 'Telegram бот не настроен на сервере.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ns = _get_or_create_settings(request.tenant, default_email=request.user.email)
        token = ns.generate_connect_token()

        return Response({
            'status': 'ok',
            'data': {
                'bot_url': f'https://t.me/{bot_username}?start={token}',
                'expires_in_minutes': 15,
            },
        })


@extend_schema(tags=['Notifications'])
class TelegramDisconnectView(APIView):
    """DELETE /api/v1/notifications/settings/telegram/ — отвязать Telegram."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(request=None, responses=_NOTIFICATION_SETTINGS_RESPONSE)
    def delete(self, request):
        """Сбрасывает telegram_chat_id и username."""
        ns = _get_or_create_settings(request.tenant, default_email=request.user.email)
        ns.telegram_chat_id = ''
        ns.telegram_username = ''
        ns.connect_token = ''
        ns.connect_token_expires_at = None
        ns.save(update_fields=[
            'telegram_chat_id', 'telegram_username',
            'connect_token', 'connect_token_expires_at',
        ])
        return Response({'status': 'ok', 'data': _serialize(ns)})


@extend_schema(tags=['Notifications'])
class NotificationTestView(APIView):
    """POST /api/v1/notifications/settings/test/ — отправить тестовое Telegram-сообщение."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(request=None, responses=_NOTIFICATION_TEST_RESPONSE)
    def post(self, request):
        """Отправляет тестовое сообщение в подключённый Telegram чат тенанта."""
        ns = _get_or_create_settings(request.tenant, default_email=request.user.email)
        if not ns.telegram_chat_id:
            return Response(
                {'status': 'error', 'detail': 'Telegram не подключён.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ok = TelegramNotifier().send(
            ns.telegram_chat_id,
            f'✅ Тест MAP: уведомления для <b>{request.tenant.name}</b> работают.',
        )
        if ok:
            return Response({'status': 'ok', 'data': {'sent': True}})
        return Response(
            {'status': 'error', 'detail': 'Не удалось отправить сообщение. Проверьте токен бота.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )


@extend_schema(exclude=True)
class TelegramBotWebhookView(APIView):
    """
    POST /api/v1/notifications/webhook/telegram/ — входящие апдейты от Telegram Bot API.

    Этот эндпоинт публичный (без пользовательской авторизации).
    Защита: проверяем secret token из заголовка X-Telegram-Bot-Api-Secret-Token.
    Обрабатывает команду /start <connect_token> для привязки Telegram к тенанту.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        """Обрабатывает апдейт от Telegram: привязывает чат при /start <token>."""
        # Верификация через секрет, который мы указываем при регистрации webhook в Telegram
        secret = str(settings.TELEGRAM_BOT_TOKEN or '')
        expected = secret.rsplit(':', 1)[-1][:32] if secret else ''
        if not expected:
            # An unconfigured public receiver must never silently disable authentication.
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
        header_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not secrets.compare_digest(header_secret, expected):
            return Response(status=status.HTTP_403_FORBIDDEN)

        try:
            normalized_message = _validated_telegram_text_message(request.data)
        except TelegramUpdateValidationError:
            return Response(
                {'ok': False, 'error': 'invalid_update'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if normalized_message is None:
            return Response({'ok': True})
        text, chat_id, username = normalized_message

        # Обрабатываем только /start <token>
        if not text.startswith('/start'):
            return Response({'ok': True})

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            TelegramNotifier().send(
                chat_id,
                '👋 Это бот MAP. Для привязки перейдите в настройки уведомлений и нажмите «Подключить Telegram».',
            )
            return Response({'ok': True})

        token = parts[1].strip()
        if not token or len(token) > 64:
            TelegramNotifier().send(chat_id, '❌ Ссылка недействительна или устарела.')
            return Response({'ok': True})

        ns, consume_status = TenantNotificationSettings.consume_connect_token(
            token,
            chat_id=chat_id,
            username=username,
        )
        if consume_status == CONNECT_TOKEN_EXPIRED:
            TelegramNotifier().send(chat_id, '❌ Срок действия ссылки истёк (15 мин). Сгенерируйте новую в настройках.')
            return Response({'ok': True})
        if consume_status != CONNECT_TOKEN_CONSUMED or ns is None:
            TelegramNotifier().send(chat_id, '❌ Ссылка недействительна или устарела.')
            return Response({'ok': True})
        TelegramNotifier().send(
            chat_id,
            f'✅ Telegram подключён к организации <b>{ns.tenant.name}</b>.\n'
            f'Вы будете получать уведомления об ошибках и важных событиях.',
        )
        return Response({'ok': True})
