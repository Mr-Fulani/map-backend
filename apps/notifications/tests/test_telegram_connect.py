"""
Тесты привязки Telegram через бота (Вариант B).

Покрывает:
- модельные методы generate_connect_token / is_connect_token_valid / complete_telegram_connect
- API: GET/PUT /notifications/settings/, POST /telegram/connect/, DELETE /telegram/, POST /test/
- webhook view: корректный токен, истёкший токен, несуществующий токен, /start без токена
"""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils.timezone import now

from apps.notifications.models import TenantNotificationSettings
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_ns(tenant, **kwargs):
    return TenantNotificationSettings.objects.create(tenant=tenant, **kwargs)


def auth(raw_key):
    return {'HTTP_AUTHORIZATION': f'Bearer {raw_key}'}


# ---------------------------------------------------------------------------
# Модельные методы
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestConnectTokenModel:
    def test_generate_sets_token_and_expiry(self):
        """generate_connect_token сохраняет токен и выставляет expiry ~15 мин."""
        tenant = make_tenant('mdl-gen')
        ns = make_ns(tenant)

        token = ns.generate_connect_token()

        ns.refresh_from_db()
        assert ns.connect_token == token
        assert ns.connect_token_expires_at is not None
        delta = ns.connect_token_expires_at - now()
        assert timedelta(minutes=14) < delta <= timedelta(minutes=15)

    def test_valid_token_returns_true(self):
        """is_connect_token_valid возвращает True для свежего токена."""
        tenant = make_tenant('mdl-valid')
        ns = make_ns(tenant)
        token = ns.generate_connect_token()

        assert ns.is_connect_token_valid(token) is True

    def test_wrong_token_returns_false(self):
        """is_connect_token_valid возвращает False при несовпадении токена."""
        tenant = make_tenant('mdl-wrong')
        ns = make_ns(tenant)
        ns.generate_connect_token()

        assert ns.is_connect_token_valid('totally-wrong-token') is False

    def test_expired_token_returns_false(self):
        """is_connect_token_valid возвращает False для истёкшего токена."""
        tenant = make_tenant('mdl-exp')
        ns = make_ns(tenant)
        token = ns.generate_connect_token()
        # Сдвигаем expiry в прошлое
        ns.connect_token_expires_at = now() - timedelta(seconds=1)
        ns.save(update_fields=['connect_token_expires_at'])

        assert ns.is_connect_token_valid(token) is False

    def test_complete_connect_saves_chat_id_and_clears_token(self):
        """complete_telegram_connect сохраняет chat_id/username и обнуляет токен."""
        tenant = make_tenant('mdl-complete')
        ns = make_ns(tenant)
        ns.generate_connect_token()

        ns.complete_telegram_connect(chat_id='99887766', username='testuser')

        ns.refresh_from_db()
        assert ns.telegram_chat_id == '99887766'
        assert ns.telegram_username == 'testuser'
        assert ns.connect_token == ''
        assert ns.connect_token_expires_at is None


# ---------------------------------------------------------------------------
# GET/PUT /api/v1/notifications/settings/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestNotificationSettingsAPI:
    def test_get_returns_defaults_for_new_tenant(self, client):
        """GET создаёт настройки если их нет и возвращает telegram_connected=False."""
        tenant = make_tenant('ns-get')
        raw_key = owner_access_token(tenant)

        resp = client.get('/api/v1/notifications/settings/', **auth(raw_key))

        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['telegram_connected'] is False
        assert data['notify_on_error'] is True
        assert data['notify_on_critical'] is True

    def test_get_shows_connected_when_chat_id_set(self, client):
        """GET возвращает telegram_connected=True если chat_id уже привязан."""
        tenant = make_tenant('ns-connected')
        make_ns(tenant, telegram_chat_id='123456', telegram_username='myuser')
        raw_key = owner_access_token(tenant)

        resp = client.get('/api/v1/notifications/settings/', **auth(raw_key))

        data = resp.json()['data']
        assert data['telegram_connected'] is True
        assert data['telegram_username'] == 'myuser'

    def test_put_updates_email_and_flags(self, client):
        """PUT обновляет email и флаги, не затрагивая telegram_chat_id."""
        tenant = make_tenant('ns-put')
        make_ns(tenant, telegram_chat_id='555')
        raw_key = owner_access_token(tenant)

        resp = client.put(
            '/api/v1/notifications/settings/',
            data=json.dumps({
                'notify_email': 'alerts@co.ru',
                'notify_on_error': False,
                'notify_on_critical': True,
            }),
            content_type='application/json',
            **auth(raw_key),
        )

        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['notify_email'] == 'alerts@co.ru'
        assert data['notify_on_error'] is False
        assert data['telegram_connected'] is True  # не затронуто

    def test_unauthenticated_returns_401(self, client):
        """Без авторизации — 401."""
        resp = client.get('/api/v1/notifications/settings/')
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/settings/telegram/connect/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTelegramConnectAPI:
    def test_returns_bot_url_with_token(self, client):
        """POST возвращает bot_url содержащий токен подключения."""
        tenant = make_tenant('tg-conn')
        raw_key = owner_access_token(tenant)

        with patch('apps.notifications.views.settings') as mock_settings:
            mock_settings.TELEGRAM_BOT_USERNAME = 'TestMapBot'
            resp = client.post(
                '/api/v1/notifications/settings/telegram/connect/',
                **auth(raw_key),
            )

        assert resp.status_code == 200
        data = resp.json()['data']
        assert 'bot_url' in data
        assert 'https://t.me/TestMapBot?start=' in data['bot_url']
        assert data['expires_in_minutes'] == 15

    def test_token_is_saved_in_db(self, client):
        """После POST токен сохранён в TenantNotificationSettings."""
        tenant = make_tenant('tg-token-db')
        raw_key = owner_access_token(tenant)

        with patch('apps.notifications.views.settings') as mock_settings:
            mock_settings.TELEGRAM_BOT_USERNAME = 'TestMapBot'
            resp = client.post(
                '/api/v1/notifications/settings/telegram/connect/',
                **auth(raw_key),
            )

        bot_url = resp.json()['data']['bot_url']
        token = bot_url.split('start=')[1]

        ns = TenantNotificationSettings.objects.get(tenant=tenant)
        assert ns.connect_token == token

    def test_returns_503_when_bot_not_configured(self, client):
        """503 если TELEGRAM_BOT_USERNAME не задан."""
        tenant = make_tenant('tg-no-bot')
        raw_key = owner_access_token(tenant)

        with patch('apps.notifications.views.settings') as mock_settings:
            mock_settings.TELEGRAM_BOT_USERNAME = ''
            resp = client.post(
                '/api/v1/notifications/settings/telegram/connect/',
                **auth(raw_key),
            )

        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# DELETE /api/v1/notifications/settings/telegram/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTelegramDisconnectAPI:
    def test_clears_chat_id(self, client):
        """DELETE сбрасывает telegram_chat_id и username."""
        tenant = make_tenant('tg-disc')
        make_ns(tenant, telegram_chat_id='777', telegram_username='user7')
        raw_key = owner_access_token(tenant)

        resp = client.delete(
            '/api/v1/notifications/settings/telegram/',
            **auth(raw_key),
        )

        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['telegram_connected'] is False
        assert data['telegram_username'] == ''


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/settings/test/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestNotificationTestAPI:
    def test_sends_test_message(self, client):
        """POST /test/ отправляет сообщение если Telegram подключён."""
        tenant = make_tenant('tg-test-ok')
        make_ns(tenant, telegram_chat_id='123')
        raw_key = owner_access_token(tenant)

        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            resp = client.post(
                '/api/v1/notifications/settings/test/',
                **auth(raw_key),
            )

        assert resp.status_code == 200
        call_args = mock_tg.return_value.send.call_args
        assert call_args[0][0] == '123'
        assert isinstance(call_args[0][1], str)

    def test_returns_400_when_not_connected(self, client):
        """400 если Telegram не подключён."""
        tenant = make_tenant('tg-test-no')
        make_ns(tenant, telegram_chat_id='')
        raw_key = owner_access_token(tenant)

        resp = client.post(
            '/api/v1/notifications/settings/test/',
            **auth(raw_key),
        )

        assert resp.status_code == 400

    def test_returns_502_when_telegram_fails(self, client):
        """502 если TelegramNotifier.send вернул False."""
        tenant = make_tenant('tg-test-fail')
        make_ns(tenant, telegram_chat_id='999')
        raw_key = owner_access_token(tenant)

        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = False
            resp = client.post(
                '/api/v1/notifications/settings/test/',
                **auth(raw_key),
            )

        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/webhook/telegram/ (bot webhook)
# ---------------------------------------------------------------------------

def tg_update(text, chat_id='11111', username='tguser', update_id=1):
    """Формирует минимальный Telegram update payload."""
    return {
        'update_id': update_id,
        'message': {
            'message_id': 1,
            'chat': {'id': chat_id, 'username': username, 'type': 'private'},
            'text': text,
        },
    }


@pytest.mark.django_db
class TestTelegramBotWebhook:
    WEBHOOK_URL = '/api/v1/notifications/webhook/telegram/'

    def _post(self, client, payload, secret_header=''):
        return client.post(
            self.WEBHOOK_URL,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret_header,
        )

    def _valid_secret(self):
        """Секретный заголовок — как в TelegramBotWebhookView."""
        from django.conf import settings as dj_settings
        secret = dj_settings.TELEGRAM_BOT_TOKEN
        return secret.split(':')[-1][:32] if secret else ''

    def test_valid_token_connects_telegram(self, client):
        """Корректный /start <token> → chat_id сохранён, бот отвечает об успехе."""
        tenant = make_tenant('wh-ok')
        ns = make_ns(tenant)
        token = ns.generate_connect_token()

        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            resp = self._post(
                client,
                tg_update(f'/start {token}', chat_id='42424242', username='happyuser'),
                secret_header=self._valid_secret(),
            )

        assert resp.status_code == 200
        ns.refresh_from_db()
        assert ns.telegram_chat_id == '42424242'
        assert ns.telegram_username == 'happyuser'
        assert ns.connect_token == ''
        mock_tg.return_value.send.assert_called_once()
        sent_text = mock_tg.return_value.send.call_args[0][1]
        assert '✅' in sent_text

    def test_expired_token_does_not_connect(self, client):
        """Истёкший токен → chat_id не сохраняется, бот сообщает об истечении."""
        tenant = make_tenant('wh-exp')
        ns = make_ns(tenant)
        token = ns.generate_connect_token()
        ns.connect_token_expires_at = now() - timedelta(seconds=1)
        ns.save(update_fields=['connect_token_expires_at'])

        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            self._post(
                client,
                tg_update(f'/start {token}'),
                secret_header=self._valid_secret(),
            )

        ns.refresh_from_db()
        assert ns.telegram_chat_id == ''
        sent_text = mock_tg.return_value.send.call_args[0][1]
        assert 'истёк' in sent_text

    def test_unknown_token_does_not_connect(self, client):
        """Несуществующий токен → chat_id не сохраняется, бот сообщает об ошибке."""
        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            self._post(
                client,
                tg_update('/start nonexistent_token_xyz'),
                secret_header=self._valid_secret(),
            )

        sent_text = mock_tg.return_value.send.call_args[0][1]
        assert '❌' in sent_text

    def test_start_without_token_sends_help(self, client):
        """/start без токена → бот отвечает справкой, ничего не сохраняется."""
        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            resp = self._post(
                client,
                tg_update('/start'),
                secret_header=self._valid_secret(),
            )

        assert resp.status_code == 200
        mock_tg.return_value.send.assert_called_once()

    def test_non_start_message_is_ignored(self, client):
        """Обычное сообщение (не /start) — бот ничего не отправляет."""
        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            resp = self._post(
                client,
                tg_update('привет'),
                secret_header=self._valid_secret(),
            )

        assert resp.status_code == 200
        mock_tg.return_value.send.assert_not_called()

    def test_update_without_message_returns_ok(self, client):
        """Апдейт без message (например, channel_post) — 200 без ошибок."""
        resp = self._post(
            client,
            {'update_id': 99},
            secret_header=self._valid_secret(),
        )
        assert resp.status_code == 200

    def test_token_used_only_once(self, client):
        """Повторный /start с тем же токеном не привязывает второй chat_id."""
        tenant = make_tenant('wh-once')
        ns = make_ns(tenant)
        token = ns.generate_connect_token()

        with patch('apps.notifications.views.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            # Первый — успешный
            self._post(
                client,
                tg_update(f'/start {token}', chat_id='111'),
                secret_header=self._valid_secret(),
            )
            # Второй — токен уже сброшен
            self._post(
                client,
                tg_update(f'/start {token}', chat_id='222'),
                secret_header=self._valid_secret(),
            )

        ns.refresh_from_db()
        assert ns.telegram_chat_id == '111'  # второй не перезаписал
