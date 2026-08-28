from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from django.core.management import call_command
from django.test import Client, override_settings
from django_celery_beat.models import PeriodicTask

from apps.datasources.encryption import encrypt
from apps.marketplaces.autoload_onboarding import (
    EXHAUSTED,
    MANUAL_REVIEW,
    RETRYING,
    autoload_onboarding_presentation,
    record_autoload_onboarding_state,
)
from apps.marketplaces.feed_profile_migration import (
    FeedProfileMigrationSafetyError,
    ensure_fleet_feed_endpoint,
)
from apps.marketplaces.models import (
    AvitoAccountStatus,
    MarketplaceAccount,
    MarketplaceFeedEndpoint,
)
from apps.marketplaces.services import AvitoAccountStatusService
from apps.marketplaces.tasks import (
    _retry_or_exhaust_autoload_setup,
    recover_autoload_profile_onboarding,
    setup_autoload_profile_task,
)
from apps.tenants.models import Tenant
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


FLEET_SETTINGS = {
    'AVITO_STATUS_LIFECYCLE_MODE': 'dual_write',
    'MARKETPLACE_FEED_RUN_MODE': 'durable',
    'MARKETPLACE_FEED_INGRESS_MODE': 'dual_write',
    'MARKETPLACE_FEED_ARTIFACT_MODE': 'active',
    'MARKETPLACE_FEED_STORAGE_MODE': 'stable_bridge',
    'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED': False,
    'MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS': (),
    'MARKETPLACE_FEED_PUBLIC_BASE_URL': (
        'https://feeds.example.test/marketplace-feeds/v1/feed.xml'
    ),
    'MARKETPLACE_FEED_URL_SIGNING_KEYS': {
        'fleet-v1': b'fleet-onboarding-signing-key-material',
    },
    'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID': 'fleet-v1',
    'MEDIA_KEY_PREFIX': 'dev',
    'YC_S3_BUCKET': 'fleet-media-bucket',
    'YC_CDN_DOMAIN': '',
}


def _account(suffix: str, *, tenant: Tenant | None = None) -> MarketplaceAccount:
    if tenant is None:
        tenant = Tenant.objects.create(
            name=f'Onboarding {suffix}',
            slug=f'onboarding-{suffix}',
        )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Avito {suffix}',
        external_id=f'avito-{suffix}',
        credentials_enc=encrypt({
            'client_id': f'client-{suffix}',
            'client_secret': f'secret-{suffix}',
        }),
    )


@pytest.mark.django_db
def test_lock_contention_is_retried_and_visible():
    account = _account('lock')
    lock = MagicMock()
    lock.acquire.return_value = False

    with patch(
        'apps.marketplaces.tasks._coordination_lock',
        return_value=lock,
    ), patch.object(
        setup_autoload_profile_task,
        'retry',
        side_effect=Retry(),
    ), pytest.raises(Retry):
        setup_autoload_profile_task.run(account.pk, account.tenant_id)

    state = AvitoAccountStatus.objects.get(account=account).notification_state
    assert state['autoload_onboarding']['code'] == RETRYING
    lock.release.assert_not_called()


@pytest.mark.django_db
def test_last_retry_becomes_durable_exhausted_state():
    account = _account('exhausted')
    task = SimpleNamespace(
        request=SimpleNamespace(retries=3),
        max_retries=3,
        retry=MagicMock(),
    )

    result = _retry_or_exhaust_autoload_setup(
        task,
        account,
        exc=RuntimeError('provider unavailable'),
        reason='setup_failed',
    )

    assert result == {'status': 'exhausted', 'reason': 'setup_failed'}
    assert autoload_onboarding_presentation(account).state == 'exhausted'
    task.retry.assert_not_called()


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_safety_refusal_fences_endpoint_for_manual_review():
    account = _account('safety')
    endpoint = ensure_fleet_feed_endpoint(account)
    lock = MagicMock()
    lock.acquire.return_value = True

    with patch(
        'apps.marketplaces.tasks._coordination_lock',
        return_value=lock,
    ), patch(
        'apps.marketplaces.feed_profile_migration.run_fleet_feed_onboarding',
        side_effect=FeedProfileMigrationSafetyError('unsafe profile'),
    ):
        result = setup_autoload_profile_task.run(
            account.pk,
            account.tenant_id,
        )

    assert result == {'status': 'manual_review'}
    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW
    assert autoload_onboarding_presentation(account).state == 'manual_review'
    lock.release.assert_called_once()


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_recovery_scanner_is_bounded_and_skips_terminal_states():
    eligible = _account('eligible')
    exhausted = _account('scanner-exhausted')
    manual = _account('scanner-manual')
    inactive_tenant = Tenant.objects.create(
        name='Inactive onboarding',
        slug='inactive-onboarding',
        is_active=False,
    )
    inactive = _account('inactive', tenant=inactive_tenant)
    for account in (eligible, exhausted, manual):
        ensure_fleet_feed_endpoint(account)
    record_autoload_onboarding_state(
        exhausted,
        code=EXHAUSTED,
        message='Retry explicitly.',
    )
    record_autoload_onboarding_state(
        manual,
        code=MANUAL_REVIEW,
        message='Support required.',
    )
    assert inactive.tenant.is_active is False

    with patch(
        'apps.marketplaces.autoload_onboarding.schedule_autoload_profile_setup',
        return_value=True,
    ) as dispatch:
        result = recover_autoload_profile_onboarding(limit=1)

    assert result == {'selected': 1, 'scheduled': 1, 'dispatch_failed': 0}
    dispatch.assert_called_once_with(eligible.pk, eligible.tenant_id)

    with pytest.raises(ValueError):
        recover_autoload_profile_onboarding(limit=101)


@pytest.mark.django_db
def test_legacy_scanner_only_recovers_confirmed_dispatch_failures():
    eligible = _account('legacy-failed')
    _account('legacy-healthy')
    from apps.marketplaces.autoload_onboarding import DISPATCH_FAILED
    record_autoload_onboarding_state(
        eligible,
        code=DISPATCH_FAILED,
        message='Broker failed.',
    )

    with patch(
        'apps.marketplaces.autoload_onboarding.schedule_autoload_profile_setup',
        return_value=True,
    ) as dispatch:
        result = recover_autoload_profile_onboarding()

    assert result['selected'] == 1
    dispatch.assert_called_once_with(eligible.pk, eligible.tenant_id)


@pytest.mark.django_db
def test_health_refresh_does_not_erase_onboarding_recovery_state():
    account = _account('health-preserve')
    record_autoload_onboarding_state(
        account,
        code=EXHAUSTED,
        message='Retry explicitly.',
    )
    adapter = MagicMock()
    adapter.get_autoload_profile.return_value = {
        'autoload_enabled': True,
        'feeds_data': [],
    }
    adapter.get_tariff_info.return_value = {}
    adapter._feed_public_url.return_value = 'https://example.test/feed.xml'

    with patch(
        'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
        return_value=adapter,
    ):
        AvitoAccountStatusService.refresh(account)

    account = MarketplaceAccount.objects.select_related('avito_status').get(
        pk=account.pk,
    )
    assert autoload_onboarding_presentation(account).state == 'exhausted'


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_retry_endpoint_requeues_exhausted_but_refuses_manual_review():
    tenant, _ = TenantService.create_tenant(
        'Retry tenant',
        'retry-onboarding',
        'retry-onboarding@example.test',
        'password123',
    )
    client = Client(
        HTTP_AUTHORIZATION=f'Bearer {owner_access_token(tenant)}',
    )
    account = _account('api-retry', tenant=tenant)
    endpoint = ensure_fleet_feed_endpoint(account)
    record_autoload_onboarding_state(
        account,
        code=EXHAUSTED,
        message='Retry explicitly.',
    )

    with patch(
        'apps.marketplaces.autoload_onboarding.schedule_autoload_profile_setup',
        return_value=True,
    ) as dispatch:
        response = client.post(
            f'/api/v1/accounts/{account.pk}/autoload-status/',
        )

    assert response.status_code == 202
    assert response.json()['state'] == 'pending'
    dispatch.assert_called_once_with(account.pk, account.tenant_id)

    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        profile_state=MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW,
    )
    record_autoload_onboarding_state(
        account,
        code=MANUAL_REVIEW,
        message='Support required.',
    )
    with patch(
        'apps.marketplaces.autoload_onboarding.schedule_autoload_profile_setup',
    ) as blocked_dispatch:
        response = client.post(
            f'/api/v1/accounts/{account.pk}/autoload-status/',
        )

    assert response.status_code == 409
    assert response.json()['state'] == 'manual_review'
    blocked_dispatch.assert_not_called()


@pytest.mark.django_db
def test_periodic_setup_registers_bounded_onboarding_recovery():
    call_command('setup_periodic_tasks', stdout=StringIO())

    periodic = PeriodicTask.objects.select_related('interval').get(
        name='recover_autoload_profile_onboarding',
    )
    assert periodic.task == (
        'apps.marketplaces.tasks.recover_autoload_profile_onboarding'
    )
    assert periodic.queue == 'avito_update'
    assert periodic.interval.every == 5
    assert periodic.interval.period == 'minutes'
