from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.utils import timezone
from requests import Timeout

from apps.datasources.encryption import encrypt
from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.adapters.avito.error_handler import ForbiddenError
from apps.marketplaces.models import AvitoAccountStatus, MarketplaceAccount
from apps.marketplaces.serializers import AvitoAccountStatusSerializer
from apps.marketplaces.services import AvitoAccountStatusService
from apps.tenants.services import TenantService


def make_tenant(slug):
    """Создаёт тенанта с владельцем для тестов состояния Avito."""
    tenant, key = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    return tenant, key


def make_account(tenant):
    """Создаёт Avito-аккаунт с зашифрованными тестовыми credentials."""
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Основной',
        external_id=f'avito-{tenant.pk}',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'secret'}),
    )


def mock_adapter(profile=None, tariff=None):
    """Возвращает mock адаптера с заданными ответами profile и tariff."""
    adapter = MagicMock()
    adapter.get_autoload_profile.return_value = profile or {}
    adapter.get_tariff_info.return_value = tariff or {}
    adapter._feed_public_url.return_value = 'https://cdn.test/feed.xml'
    return adapter


@pytest.mark.django_db
class TestAvitoAccountStatusService:
    def test_http_200_with_disabled_profile_is_not_active(self):
        """Поле autoload_enabled=false важнее успешного HTTP-статуса."""
        tenant, _ = make_tenant('avito-disabled')
        account = make_account(tenant)
        adapter = mock_adapter(
            profile={
                'autoload_enabled': False,
                'feeds_data': [{'feed_url': 'https://cdn.test/feed.xml'}],
            },
        )

        with patch(
            'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
            return_value=adapter,
        ):
            result = AvitoAccountStatusService.refresh(account)

        assert result.connection_status == AvitoAccountStatus.CONNECTION_CONNECTED
        assert result.autoload_status == AvitoAccountStatus.AUTOLOAD_DISABLED
        assert result.feed_configured is True
        account.refresh_from_db()
        assert account.autoload_active is False

    def test_active_tariff_dates_and_packages_are_saved(self):
        """Сервис сохраняет срок, цену и остатки текущего тарифа."""
        tenant, _ = make_tenant('avito-tariff')
        account = make_account(tenant)
        starts_at = timezone.now() - timedelta(days=10)
        ends_at = timezone.now() + timedelta(days=20)
        adapter = mock_adapter(
            profile={'autoload_enabled': True, 'feeds_data': []},
            tariff={
                'current': {
                    'isActive': True,
                    'level': 'Тариф «Максимальный»',
                    'startTime': int(starts_at.timestamp()),
                    'closeTime': int(ends_at.timestamp()),
                    'price': {'price': 12345.67},
                    'packages': [{
                        'categories': [{'name': 'Запчасти и аксессуары'}],
                        'locations': ['Москва'],
                        'remain': 80,
                        'total': 100,
                        'priceConditions': [{'total': 100}],
                    }],
                },
                'scheduled': {
                    'level': 'Тариф «Расширенный»',
                    'startTime': int(ends_at.timestamp()),
                    'price': {'price': 15000},
                },
            },
        )

        with patch(
            'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
            return_value=adapter,
        ):
            result = AvitoAccountStatusService.refresh(account)

        assert result.tariff_status == AvitoAccountStatus.TARIFF_ACTIVE
        assert result.tariff_name == 'Тариф «Максимальный»'
        assert str(result.tariff_price) == '12345.67'
        assert result.placement_packages[0]['remain'] == 80
        assert 'priceConditions' not in result.placement_packages[0]
        assert result.scheduled_tariff['name'] == 'Тариф «Расширенный»'

    def test_temporary_error_keeps_last_confirmed_state(self):
        """Timeout не превращает активную Автозагрузку и тариф в неактивные."""
        tenant, _ = make_tenant('avito-timeout')
        account = make_account(tenant)
        saved = AvitoAccountStatus.objects.create(
            tenant=tenant,
            account=account,
            connection_status=AvitoAccountStatus.CONNECTION_CONNECTED,
            autoload_status=AvitoAccountStatus.AUTOLOAD_ENABLED,
            tariff_status=AvitoAccountStatus.TARIFF_ACTIVE,
            tariff_name='Сохранённый тариф',
        )
        adapter = mock_adapter()
        adapter.get_autoload_profile.side_effect = Timeout()
        adapter.get_tariff_info.side_effect = Timeout()

        with patch(
            'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
            return_value=adapter,
        ):
            result = AvitoAccountStatusService.refresh(account)

        assert result.pk == saved.pk
        assert result.connection_status == AvitoAccountStatus.CONNECTION_UNAVAILABLE
        assert result.autoload_status == AvitoAccountStatus.AUTOLOAD_ENABLED
        assert result.tariff_status == AvitoAccountStatus.TARIFF_ACTIVE
        assert result.tariff_name == 'Сохранённый тариф'
        assert result.last_error_code == 'tariff_unavailable'

    def test_forbidden_tariff_clears_previous_contract(self):
        """Подтверждённый 403 не оставляет устаревшие реквизиты тарифа."""
        tenant, _ = make_tenant('avito-tariff-forbidden')
        account = make_account(tenant)
        AvitoAccountStatus.objects.create(
            tenant=tenant,
            account=account,
            tariff_status=AvitoAccountStatus.TARIFF_ACTIVE,
            tariff_name='Старый тариф',
            tariff_ends_at=timezone.now() + timedelta(days=30),
            placement_packages=[{'remain': 100, 'total': 100}],
        )
        adapter = mock_adapter(profile={'autoload_enabled': True})
        adapter.get_tariff_info.side_effect = ForbiddenError('Нет доступа')

        with patch(
            'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
            return_value=adapter,
        ):
            result = AvitoAccountStatusService.refresh(account)

        assert result.tariff_status == AvitoAccountStatus.TARIFF_NOT_FOUND
        assert result.tariff_name == ''
        assert result.tariff_ends_at is None
        assert result.placement_packages == []

    def test_notifications_are_not_repeated_for_same_thresholds(self):
        """Одинаковые пороги срока и размещений уведомляют только один раз."""
        tenant, _ = make_tenant('avito-notification-dedup')
        account = make_account(tenant)
        status_obj = AvitoAccountStatus.objects.create(
            tenant=tenant,
            account=account,
            autoload_status=AvitoAccountStatus.AUTOLOAD_ENABLED,
            tariff_status=AvitoAccountStatus.TARIFF_ACTIVE,
            tariff_ends_at=timezone.now() + timedelta(days=7),
            placement_packages=[{'remain': 10, 'total': 100}],
        )

        with patch.object(
            AvitoAccountStatusService, '_queue_notification',
        ) as queue_notification:
            AvitoAccountStatusService._notify_thresholds(status_obj)
            status_obj.refresh_from_db()
            AvitoAccountStatusService._notify_thresholds(status_obj)

        assert queue_notification.call_count == 2
        status_obj.refresh_from_db()
        assert status_obj.notification_state['expiry'] == 7
        assert status_obj.notification_state['placements'] == 10

    def test_auth_and_inactive_tariff_notifications_are_deduplicated(self):
        """Критичные состояния подключения и тарифа не создают спам."""
        tenant, _ = make_tenant('avito-critical-dedup')
        account = make_account(tenant)
        status_obj = AvitoAccountStatus.objects.create(
            tenant=tenant,
            account=account,
            connection_status=AvitoAccountStatus.CONNECTION_AUTH_ERROR,
            autoload_status=AvitoAccountStatus.AUTOLOAD_UNKNOWN,
            tariff_status=AvitoAccountStatus.TARIFF_INACTIVE,
        )

        with patch.object(
            AvitoAccountStatusService, '_queue_notification',
        ) as queue_notification:
            AvitoAccountStatusService._notify_thresholds(status_obj)
            status_obj.refresh_from_db()
            AvitoAccountStatusService._notify_thresholds(status_obj)

        assert queue_notification.call_count == 2
        assert status_obj.notification_state['connection'] == 'auth_error'
        assert status_obj.notification_state['tariff'] == 'inactive'


@pytest.mark.django_db
class TestAvitoAccountStatusSerializer:
    def test_calculates_days_and_placement_totals(self):
        """Дни и суммарные лимиты вычисляются из актуального снимка."""
        tenant, _ = make_tenant('avito-serialize')
        account = make_account(tenant)
        status_obj = AvitoAccountStatus.objects.create(
            tenant=tenant,
            account=account,
            tariff_status=AvitoAccountStatus.TARIFF_ACTIVE,
            tariff_ends_at=timezone.now() + timedelta(days=5),
            placement_packages=[
                {'remain': 30, 'total': 50},
                {'remain': 10, 'total': 25},
            ],
            profile_checked_at=timezone.now(),
            tariff_checked_at=timezone.now(),
        )

        data = AvitoAccountStatusSerializer(status_obj).data

        assert data['days_left'] == 5
        assert data['placements_remaining'] == 40
        assert data['placements_total'] == 75
        assert data['profile_stale'] is False
        assert data['tariff_stale'] is False


@pytest.mark.django_db
class TestAvitoAccountStatusAPI:
    def test_manual_check_returns_compatible_and_detailed_status(self):
        """Endpoint сохраняет activated и добавляет подробный снимок тарифа."""
        tenant, key = make_tenant('avito-status-api')
        account = make_account(tenant)
        adapter = mock_adapter(
            profile={'autoload_enabled': False, 'feeds_data': []},
            tariff={'current': {'isActive': False, 'level': 'Тариф завершён'}},
        )

        with patch(
            'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
            return_value=adapter,
        ):
            response = Client().get(
                f'/api/v1/accounts/{account.pk}/autoload-status/',
                HTTP_AUTHORIZATION=f'Bearer {key}',
            )

        assert response.status_code == 200
        data = response.json()
        assert data['activated'] is False
        assert data['status']['autoload_status'] == 'disabled'
        assert data['status']['tariff_status'] == 'inactive'
        assert data['activate_url'].endswith('/autoload/settings')

    def test_manual_check_does_not_access_another_tenant(self):
        """Tenant не может обновить или прочитать чужой Avito-аккаунт."""
        first, _ = make_tenant('avito-status-owner')
        second, second_key = make_tenant('avito-status-other')
        account = make_account(first)

        response = Client().get(
            f'/api/v1/accounts/{account.pk}/autoload-status/',
            HTTP_AUTHORIZATION=f'Bearer {second_key}',
        )

        assert response.status_code == 404


def test_adapter_uses_autoload_enabled_field():
    """Проверка перед публикацией учитывает тело ответа Avito."""
    adapter = object.__new__(AvitoAdapter)
    adapter.get_autoload_profile = MagicMock(return_value={'autoload_enabled': False})

    assert adapter.is_autoload_active() is False


def test_setup_profile_preserves_customer_settings():
    """Подключение MAP не включает профиль и не стирает настройки клиента."""
    adapter = object.__new__(AvitoAdapter)
    adapter.account = MagicMock(name='account')
    adapter.account.name = 'Основной'
    adapter._feed_public_url = MagicMock(return_value='https://cdn.test/map.xml')
    adapter.get_autoload_profile = MagicMock(return_value={
        'autoload_enabled': False,
        'report_email': 'owner@example.com',
        'feeds_data': [
            {'feed_name': 'Сторонний фид', 'feed_url': 'https://other.test/feed.xml'},
            {'feed_name': 'Старый MAP', 'feed_url': 'https://cdn.test/map.xml'},
        ],
        'schedule': [{'rate': 1000, 'weekdays': [1], 'time_slots': [9]}],
    })
    adapter._request = MagicMock()

    adapter.setup_autoload_profile('fallback@example.com')

    payload = adapter._request.call_args.kwargs['json']
    assert payload['autoload_enabled'] is False
    assert payload['report_email'] == 'owner@example.com'
    assert payload['schedule'] == [
        {'rate': 1000, 'weekdays': [1], 'time_slots': [9]},
    ]
    assert payload['feeds_data'] == [
        {'feed_name': 'Сторонний фид', 'feed_url': 'https://other.test/feed.xml'},
        {'feed_name': 'MAP feed — Основной', 'feed_url': 'https://cdn.test/map.xml'},
    ]


@pytest.mark.django_db
def test_refresh_task_is_tenant_scoped_and_releases_lock():
    """Фоновая задача не читает аккаунт другого тенанта и освобождает lock."""
    from apps.marketplaces.tasks import refresh_avito_account_status_task

    owner, _ = make_tenant('avito-task-owner')
    other, _ = make_tenant('avito-task-other')
    account = make_account(owner)

    with patch('apps.marketplaces.tasks.cache') as cache:
        lock = cache.lock.return_value
        lock.acquire.return_value = True
        result = refresh_avito_account_status_task(account.pk, other.pk)

    assert result == {'status': 'not_found'}
    lock.acquire.assert_called_once_with(blocking=False)
    lock.release.assert_called_once_with()
