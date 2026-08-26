from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.marketplaces.adapters.avito.profile_migration import (
    AvitoProfileMigrationClient,
    PreparedAvitoProfilePost,
    build_profile_plan,
    validate_avito_profile,
)
from apps.marketplaces.feed_endpoint import marketplace_feed_public_url
from apps.marketplaces.feed_profile_migration import (
    FeedProfileMigrationProviderUncertain,
    FeedProfileMigrationSafetyError,
    ensure_fleet_feed_endpoint,
    fleet_feed_onboarding_ready,
    run_fleet_feed_onboarding,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.marketplaces.serializers import MarketplaceAccountSerializer
from apps.marketplaces.services import (
    AvitoAccountStatusService,
    MarketplaceAccountService,
)
from apps.tenants.models import Tenant


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


def _account(slug: str) -> MarketplaceAccount:
    tenant = Tenant.objects.create(name=f'Fleet {slug}', slug=slug)
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Avito {slug}',
        external_id=f'avito-{slug}',
        credentials_enc=b'opaque-fleet-credentials',
    )


def _source_profile(endpoint: MarketplaceFeedEndpoint) -> dict:
    return {
        'agreement': True,
        'autoload_enabled': False,
        'allow_pay_over_limit': False,
        'report_email': 'owner@example.test',
        'feeds_data': [
            {
                'feed_name': 'Foreign feed',
                'feed_url': 'https://foreign.example.test/feed.xml',
            },
            {
                'feed_name': 'Existing MAP feed',
                'feed_url': endpoint.legacy_profile_url,
            },
        ],
        'schedule': [{'rate': 123, 'weekdays': [1], 'time_slots': [9]}],
        'uploadMode': 'auto',
    }


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_endpoint_is_reserved_synchronously_and_idempotently():
    account = _account('reserve')

    first = ensure_fleet_feed_endpoint(account)
    second = ensure_fleet_feed_endpoint(account)

    assert first is not None
    assert second is not None
    assert first.pk == second.pk
    assert MarketplaceFeedEndpoint.objects.filter(account=account).count() == 1
    assert first.storage_mode == MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
    assert first.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW
    assert first.serve_enabled is False
    assert first.owner_identity_digest == account_identity_digest(account)
    assert first.legacy_object_key.endswith(f'-{account.pk}/feed.xml')


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_new_account_response_is_managed_before_background_onboarding():
    tenant = Tenant.objects.create(name='Fleet create', slug='fleet-create')

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value='avito-fleet-create',
    ):
        account = MarketplaceAccountService.create(tenant, {
            'marketplace': MarketplaceAccount.MARKETPLACE_AVITO,
            'name': 'New Avito account',
            'client_id': 'client-id',
            'client_secret': 'client-secret',
        })

    endpoint = MarketplaceFeedEndpoint.objects.get(account=account)
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW
    assert MarketplaceAccountSerializer(account).data[
        'feed_endpoint_managed'
    ] is True


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_onboarding_replaces_only_map_url_and_verifies_stable_endpoint():
    account = _account('profile')
    endpoint = ensure_fleet_feed_endpoint(account)
    assert endpoint is not None
    source = _source_profile(endpoint)
    observation = build_profile_plan(
        account=account,
        profile=source,
        source_url=endpoint.legacy_profile_url,
        source_object_key=endpoint.legacy_object_key,
        stable_url=marketplace_feed_public_url(endpoint),
    )
    client = MagicMock(spec=AvitoProfileMigrationClient)
    client.adapter = MagicMock()
    client.adapter.get_autoload_profile.return_value = deepcopy(source)
    client.prepare_post.return_value = PreparedAvitoProfilePost('token')
    client.get_profile.return_value = deepcopy(observation.plan.target_profile)

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        result = run_fleet_feed_onboarding(
            tenant_id=account.tenant_id,
            account_id=account.pk,
            report_email='fallback@example.test',
        )

    endpoint.refresh_from_db()
    assert result == 'verified'
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
    assert endpoint.serve_enabled is True
    assert fleet_feed_onboarding_ready(account.pk) is True
    posted = client.post_profile_once.call_args.args[1]
    assert posted == observation.plan.target_profile
    assert posted['autoload_enabled'] is False
    assert posted['report_email'] == 'owner@example.test'
    assert posted['schedule'] == source['schedule']
    assert posted['feeds_data'][0] == source['feeds_data'][0]


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_onboarding_reconciles_visible_target_without_another_post():
    account = _account('visible-target')
    endpoint = ensure_fleet_feed_endpoint(account)
    assert endpoint is not None
    source = _source_profile(endpoint)
    observation = build_profile_plan(
        account=account,
        profile=source,
        source_url=endpoint.legacy_profile_url,
        source_object_key=endpoint.legacy_object_key,
        stable_url=marketplace_feed_public_url(endpoint),
    )
    client = MagicMock(spec=AvitoProfileMigrationClient)
    client.adapter = MagicMock()
    client.adapter.get_autoload_profile.return_value = deepcopy(
        observation.plan.target_profile,
    )

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        result = run_fleet_feed_onboarding(
            tenant_id=account.tenant_id,
            account_id=account.pk,
            report_email='fallback@example.test',
        )

    assert result == 'verified'
    assert fleet_feed_onboarding_ready(account.pk) is True
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_unknown_profile_post_is_never_replayed_blindly():
    account = _account('unknown-post')
    endpoint = ensure_fleet_feed_endpoint(account)
    assert endpoint is not None
    source = _source_profile(endpoint)
    source_fingerprint = validate_avito_profile(source).fingerprint
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        serve_enabled=True,
        profile_state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        profile_fingerprint=source_fingerprint,
        profile_revision=2,
        profile_verified_at=timezone.now(),
    )
    client = MagicMock(spec=AvitoProfileMigrationClient)
    client.adapter = MagicMock()
    client.adapter.get_autoload_profile.return_value = deepcopy(source)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationProviderUncertain),
    ):
        run_fleet_feed_onboarding(
            tenant_id=account.tenant_id,
            account_id=account.pk,
            report_email='fallback@example.test',
        )

    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_onboarding_refuses_mixed_or_drifting_owned_feeds():
    account = _account('mixed-owned')
    endpoint = ensure_fleet_feed_endpoint(account)
    assert endpoint is not None
    mixed = _source_profile(endpoint)
    mixed['feeds_data'].append({
        'feed_name': 'Duplicate MAP stable feed',
        'feed_url': marketplace_feed_public_url(endpoint),
    })
    client = MagicMock(spec=AvitoProfileMigrationClient)
    client.adapter = MagicMock()
    client.adapter.get_autoload_profile.return_value = mixed

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationSafetyError),
    ):
        run_fleet_feed_onboarding(
            tenant_id=account.tenant_id,
            account_id=account.pk,
            report_email='fallback@example.test',
        )

    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**FLEET_SETTINGS)
def test_account_health_checks_managed_url_without_legacy_fallback():
    account = _account('health')
    endpoint = ensure_fleet_feed_endpoint(account)
    assert endpoint is not None
    source = _source_profile(endpoint)
    observation = build_profile_plan(
        account=account,
        profile=source,
        source_url=endpoint.legacy_profile_url,
        source_object_key=endpoint.legacy_object_key,
        stable_url=marketplace_feed_public_url(endpoint),
    )
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        serve_enabled=True,
        profile_state=MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        profile_fingerprint=observation.target_fingerprint,
        profile_revision=2,
        profile_verified_at=timezone.now(),
    )
    adapter = MagicMock()
    adapter.get_autoload_profile.return_value = deepcopy(
        observation.plan.target_profile,
    )
    adapter.get_tariff_info.return_value = {}

    with patch(
        'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
        return_value=adapter,
    ):
        status = AvitoAccountStatusService.refresh(account)

    assert status.feed_configured is True
    adapter._feed_public_url.assert_not_called()
