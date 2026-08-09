import logging

from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notifications.models import TenantNotificationSettings
from apps.notifications.telegram import TelegramNotifier
from apps.tenants.permissions import TenantAdminPermission, TenantAdminWritePermission

logger = logging.getLogger(__name__)


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
        if not bot_username:
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

    Этот эндпоинт публичный (без авторизации) — Telegram не умеет передавать токены.
    Защита: проверяем секретный токен из заголовка X-Telegram-Bot-Api-Secret-Token.
    Обрабатывает команду /start <connect_token> для привязки Telegram к тенанту.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        """Обрабатывает апдейт от Telegram: привязывает чат при /start <token>."""
        # Верификация через секрет, который мы указываем при регистрации webhook в Telegram
        secret = settings.TELEGRAM_BOT_TOKEN
        header_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        # Токен бота имеет вид "12345:ABCdef..." — берём часть после ":",
        # она всегда состоит только из допустимых символов.
        # Должно совпадать со значением из setup_telegram_webhook.
        expected = secret.split(':')[-1][:32] if secret else ''
        if expected and header_secret != expected:
            return Response(status=status.HTTP_403_FORBIDDEN)

        update = request.data
        message = update.get('message') or update.get('edited_message')
        if not message:
            return Response({'ok': True})

        text = message.get('text', '').strip()
        chat = message.get('chat', {})
        chat_id = str(chat.get('id', ''))
        username = chat.get('username', '') or chat.get('first_name', '')

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
        try:
            ns = TenantNotificationSettings.objects.select_related('tenant').get(
                connect_token=token,
            )
        except TenantNotificationSettings.DoesNotExist:
            TelegramNotifier().send(chat_id, '❌ Ссылка недействительна или устарела.')
            return Response({'ok': True})

        if not ns.is_connect_token_valid(token):
            TelegramNotifier().send(chat_id, '❌ Срок действия ссылки истёк (15 мин). Сгенерируйте новую в настройках.')
            return Response({'ok': True})

        ns.complete_telegram_connect(chat_id, username)
        TelegramNotifier().send(
            chat_id,
            f'✅ Telegram подключён к организации <b>{ns.tenant.name}</b>.\n'
            f'Вы будете получать уведомления об ошибках и важных событиях.',
        )
        return Response({'ok': True})
