from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.marketplaces.adapters.avito.profile_migration import (
    AvitoProfileMigrationClient,
    AvitoProfilePostError,
    AvitoProfileTransportError,
    AvitoProfileValidationError,
    PreparedAvitoProfilePost,
    build_profile_plan,
    inspect_unprovisioned_profile,
    is_profile_feed_configured,
    observe_endpoint_profile,
    probe_feed_bridge_parity,
    validate_avito_profile,
)
from apps.marketplaces.feed_endpoint import marketplace_feed_public_url
from apps.marketplaces.feed_profile_migration import (
    FeedProfileMigrationConflict,
    FeedProfileMigrationError,
    FeedProfileMigrationProviderUncertain,
    FeedProfileMigrationSafetyError,
    run_feed_profile_migration,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.tenants.models import Tenant


SIGNING_KEY = b'profile-migration-test-signing-key-material'
SIGNING_KEY_V2 = b'profile-migration-second-signing-key-material'
MIGRATION_SETTINGS = {
    'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED': True,
    'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS': 300,
    'MARKETPLACE_FEED_PUBLIC_BASE_URL': (
        'https://feeds.example.test/marketplace-feeds/v1/feed.xml'
    ),
    'MARKETPLACE_FEED_URL_SIGNING_KEYS': {'profile-v1': SIGNING_KEY},
    'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID': 'profile-v1',
    'MEDIA_KEY_PREFIX': 'dev',
    'YC_S3_BUCKET': 'profile-feed-bucket',
    'YC_CDN_DOMAIN': '',
}
ROTATION_SETTINGS = {
    **MIGRATION_SETTINGS,
    'MARKETPLACE_FEED_URL_SIGNING_KEYS': {
        'profile-v1': SIGNING_KEY,
        'profile-v2': SIGNING_KEY_V2,
    },
    'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID': 'profile-v2',
}


def _inside_application_atomic_block() -> bool:
    return any(
        not getattr(block, '_from_testcase', False)
        for block in connection.atomic_blocks
    )


def _account(slug: str) -> tuple[Tenant, MarketplaceAccount]:
    tenant = Tenant.objects.create(name=f'Tenant {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Migration account',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-profile-test-credentials',
    )
    return tenant, account


def _legacy_locator(account: MarketplaceAccount) -> tuple[str, str]:
    key = (
        f'dev/feeds/{account.tenant.slug}/avito/'
        f'migration-account-{account.pk}/feed.xml'
    )
    return (
        key,
        f'https://storage.yandexcloud.net/profile-feed-bucket/{key}',
    )


def _profile(
    account: MarketplaceAccount,
    *,
    report_email: str | None = 'reports@example.test',
) -> dict:
    _key, legacy_url = _legacy_locator(account)
    return {
        'autoload_enabled': False,
        'report_email': report_email,
        'feeds_data': [
            {
                'feed_name': 'Foreign before',
                'feed_url': 'https://foreign.example/before.xml',
                'foreign_extension': {'keep': ['exact', 1]},
            },
            {
                'feed_name': 'MAP legacy name must stay unchanged',
                'feed_url': legacy_url,
            },
            {
                'feed_name': 'Foreign after',
                'feed_url': 'https://foreign.example/after.xml',
            },
        ],
        'schedule': [],
    }


def _endpoint(
    account: MarketplaceAccount,
    *,
    state: str = MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    revision: int = 4,
    profile_snapshot: dict | None = None,
) -> MarketplaceFeedEndpoint:
    profile_snapshot = profile_snapshot or _profile(account)
    key, legacy_url = _legacy_locator(account)
    return MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='profile-v1',
        owner_identity_digest=account_identity_digest(account),
        serve_enabled=True,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        legacy_object_key=key,
        legacy_profile_url=legacy_url,
        profile_state=state,
        profile_fingerprint=validate_avito_profile(profile_snapshot).fingerprint,
        profile_revision=revision,
        profile_verified_at=timezone.now(),
    )


def _client(profile: dict) -> MagicMock:
    client = MagicMock(spec=AvitoProfileMigrationClient)
    client.get_profile.return_value = deepcopy(profile)
    return client


def test_full_profile_validation_requires_complete_upsert_shape():
    complete = {
        'allow_pay_over_limit': False,
        'autoload_enabled': False,
        'report_email': None,
        'feeds_data': [{'feed_name': 'One', 'feed_url': 'https://one.test/feed.xml'}],
        'schedule': [],
        'uploadMode': 'auto',
    }
    assert validate_avito_profile(complete).profile == complete

    for missing in ('autoload_enabled', 'report_email', 'feeds_data', 'schedule'):
        invalid = deepcopy(complete)
        invalid.pop(missing)
        with pytest.raises(AvitoProfileValidationError, match='missing required'):
            validate_avito_profile(invalid)

    invalid_feed = deepcopy(complete)
    invalid_feed['feeds_data'][0].pop('feed_name')
    with pytest.raises(AvitoProfileValidationError, match='feed name'):
        validate_avito_profile(invalid_feed)

    invalid_pay_over_limit = deepcopy(complete)
    invalid_pay_over_limit['allow_pay_over_limit'] = 0
    with pytest.raises(AvitoProfileValidationError, match='allow_pay_over_limit'):
        validate_avito_profile(invalid_pay_over_limit)

    invalid_upload_mode = deepcopy(complete)
    invalid_upload_mode['uploadMode'] = 'unexpected'
    with pytest.raises(AvitoProfileValidationError, match='uploadMode'):
        validate_avito_profile(invalid_upload_mode)


def test_profile_fingerprint_normalizes_only_unordered_schedule_values():
    profile = {
        'autoload_enabled': False,
        'report_email': 'reports@example.test',
        'feeds_data': [
            {'feed_name': 'One', 'feed_url': 'https://one.test/feed.xml'},
        ],
        'schedule': [
            {
                'time_slots': ['18:00-19:00', '09:00-10:00'],
                'weekdays': ['sunday', 'monday'],
                'provider_metadata': ['order', 'must', 'remain'],
            },
        ],
    }
    reordered = deepcopy(profile)
    reordered['schedule'][0]['time_slots'].reverse()
    reordered['schedule'][0]['weekdays'].reverse()

    original_snapshot = validate_avito_profile(profile)
    reordered_snapshot = validate_avito_profile(reordered)

    assert original_snapshot.fingerprint == reordered_snapshot.fingerprint
    assert original_snapshot.profile == profile
    assert reordered_snapshot.profile == reordered

    changed_value = deepcopy(profile)
    changed_value['schedule'][0]['weekdays'][0] = 'tuesday'
    assert (
        validate_avito_profile(changed_value).fingerprint
        != original_snapshot.fingerprint
    )

    changed_unrelated_order = deepcopy(profile)
    changed_unrelated_order['schedule'][0]['provider_metadata'].reverse()
    assert (
        validate_avito_profile(changed_unrelated_order).fingerprint
        != original_snapshot.fingerprint
    )


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_plan_changes_only_owned_url_and_preserves_full_profile_exactly():
    _tenant, account = _account('profile-plan')
    endpoint = _endpoint(account)
    source = _profile(account, report_email=None)
    source['allow_pay_over_limit'] = False
    source['uploadMode'] = 'auto'
    stable_url = marketplace_feed_public_url(endpoint)

    observation = build_profile_plan(
        account=account,
        profile=source,
        source_url=endpoint.legacy_profile_url,
        source_object_key=endpoint.legacy_object_key,
        stable_url=stable_url,
    )

    assert observation.outcome == 'source'
    target = observation.plan.target_profile
    assert list(target) == list(source)
    assert target['autoload_enabled'] is False
    assert target['report_email'] is None
    assert target['schedule'] == []
    assert target['allow_pay_over_limit'] is False
    assert target['uploadMode'] == 'auto'
    assert target['feeds_data'][0] == source['feeds_data'][0]
    assert target['feeds_data'][2] == source['feeds_data'][2]
    assert target['feeds_data'][1] == {
        **source['feeds_data'][1],
        'feed_url': stable_url,
    }
    assert 'agreement' not in target

    reverse = build_profile_plan(
        account=account,
        profile=target,
        source_url=endpoint.legacy_profile_url,
        source_object_key=endpoint.legacy_object_key,
        stable_url=stable_url,
    )
    assert reverse.outcome == 'target'
    assert reverse.source_fingerprint == observation.source_fingerprint
    assert reverse.target_fingerprint == observation.target_fingerprint
    assert reverse.plan.source_profile == source


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_inspect_accepts_exact_historical_unprefixed_map_feed():
    _tenant, account = _account('historical-profile')
    source = _profile(account)
    historical_key = (
        f'feeds/{account.tenant.slug}/{account.marketplace}/'
        f'migration-account-{account.pk}/feed.xml'
    )
    source['feeds_data'][1]['feed_url'] = (
        f'https://storage.yandexcloud.net/profile-feed-bucket/'
        f'{historical_key}'
    )

    plan = inspect_unprovisioned_profile(account, source)

    assert plan.owned_feed_count == 1
    assert plan.foreign_feed_count == 2
    assert plan.source_object_key == historical_key


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_untrusted_frozen_legacy_locator_never_builds_a_plan():
    _tenant, account = _account('profile-untrusted')
    endpoint = _endpoint(account)
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        legacy_profile_url='https://attacker.example/feed.xml',
    )
    endpoint.refresh_from_db()

    with pytest.raises(AvitoProfileValidationError, match='not trusted'):
        observe_endpoint_profile(endpoint, _profile(account))


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_prepare_provisions_resumable_checkpoint_then_proves_parity():
    tenant, account = _account('profile-prepare')
    profile = _profile(account)
    source_fingerprint = validate_avito_profile(profile).fingerprint
    client = _client(profile)
    client.get_profile.side_effect = [deepcopy(profile), deepcopy(profile)]

    def assert_provisional(checkpoint):
        assert _inside_application_atomic_block() is False
        checkpoint.refresh_from_db()
        assert checkpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.MIGRATING
        assert checkpoint.serve_enabled is True
        assert checkpoint.profile_revision == 1
        return SimpleNamespace(content_fingerprint='a' * 64, byte_count=123)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.probe_feed_bridge_parity',
            side_effect=assert_provisional,
        ) as probe,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='prepare',
            expected_revision=0,
            expected_source_fingerprint=source_fingerprint,
            apply=True,
        )

    endpoint = MarketplaceFeedEndpoint.objects.get(account=account)
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    assert endpoint.profile_revision == 2
    assert endpoint.profile_fingerprint == source_fingerprint
    assert endpoint.legacy_profile_url == _legacy_locator(account)[1]
    assert result.state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    assert result.parity_verified is True
    assert result.target_fingerprint
    assert client.get_profile.call_count == 2
    probe.assert_called_once()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_prepare_without_endpoint_fences_identity_change_during_profile_get():
    tenant, account = _account('profile-prepare-identity-race')
    profile = _profile(account)
    fingerprint = validate_avito_profile(profile).fingerprint
    client = _client(profile)

    def read_from_old_generation():
        MarketplaceAccount.all_objects.filter(pk=account.pk).update(
            external_id='new-provider-generation',
        )
        return deepcopy(profile)

    client.get_profile.side_effect = read_from_old_generation
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.probe_feed_bridge_parity',
        ) as probe,
        pytest.raises(FeedProfileMigrationConflict, match='identity changed'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='prepare',
            expected_revision=0,
            expected_source_fingerprint=fingerprint,
            apply=True,
        )

    assert not MarketplaceFeedEndpoint.objects.filter(account_id=account.pk).exists()
    probe.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_prepare_refuses_unwritable_profile_before_sticky_checkpoint_or_parity():
    tenant, account = _account('profile-prepare-null-report-email')
    profile = _profile(account, report_email=None)
    fingerprint = validate_avito_profile(profile).fingerprint
    client = _client(profile)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.probe_feed_bridge_parity',
        ) as probe,
        pytest.raises(FeedProfileMigrationSafetyError, match='full upsert'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='prepare',
            expected_revision=0,
            expected_source_fingerprint=fingerprint,
            apply=True,
        )

    assert not MarketplaceFeedEndpoint.objects.filter(account_id=account.pk).exists()
    probe.assert_not_called()
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_prepare_existing_checkpoint_fences_signing_key_rotation_during_get():
    tenant, account = _account('profile-prepare-key-race')
    profile = _profile(account)
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.MIGRATING,
    )
    client = _client(profile)

    def rotate_key_before_checkpoint_lock():
        MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
            token_key_id='profile-v2',
        )
        return deepcopy(profile)

    client.get_profile.side_effect = rotate_key_before_checkpoint_lock
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.probe_feed_bridge_parity',
        ) as probe,
        pytest.raises(FeedProfileMigrationConflict, match='generation changed'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='prepare',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.MIGRATING
    assert endpoint.token_key_id == 'profile-v2'
    probe.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_prepare_crash_stays_migrating_and_source_confirmation_is_safe():
    tenant, account = _account('profile-prepare-crash')
    profile = _profile(account)
    fingerprint = validate_avito_profile(profile).fingerprint
    failed_client = _client(profile)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=failed_client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.probe_feed_bridge_parity',
            side_effect=AvitoProfileTransportError('secret URL must not escape'),
        ),
        pytest.raises(FeedProfileMigrationError, match='failed safely'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='prepare',
            expected_revision=0,
            expected_source_fingerprint=fingerprint,
            apply=True,
        )

    checkpoint = MarketplaceFeedEndpoint.objects.get(account=account)
    assert checkpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.MIGRATING
    assert checkpoint.profile_revision == 1
    frozen = (checkpoint.legacy_object_key, checkpoint.legacy_profile_url)

    confirm_client = _client(profile)
    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=confirm_client,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='confirm-prepare-source',
            expected_revision=1,
            expected_source_fingerprint=fingerprint,
            apply=True,
        )

    checkpoint.refresh_from_db()
    assert checkpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.MIGRATING
    assert checkpoint.profile_revision == 2
    assert (checkpoint.legacy_object_key, checkpoint.legacy_profile_url) == frozen
    assert result.verification_outcome == 'source_confirmed_prepare_checkpoint'

    blocked = _client(profile)
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=blocked,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='not ready'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=2,
            expected_source_fingerprint=fingerprint,
            apply=True,
        )
    blocked.get_profile.assert_not_called()

    retry_client = _client(profile)
    retry_client.get_profile.side_effect = [deepcopy(profile), deepcopy(profile)]
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=retry_client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.probe_feed_bridge_parity',
            return_value=SimpleNamespace(content_fingerprint='b' * 64, byte_count=123),
        ) as retry_probe,
    ):
        retry_result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='prepare',
            expected_revision=2,
            expected_source_fingerprint=fingerprint,
            apply=True,
        )
    checkpoint.refresh_from_db()
    assert checkpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    assert checkpoint.profile_revision == 3
    assert retry_result.parity_verified is True
    retry_probe.assert_called_once()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_migrate_commits_update_unknown_before_exactly_one_post_and_preserves_payload():
    tenant, account = _account('profile-migrate')
    endpoint = _endpoint(account)
    source = _profile(account)
    client = _client(source)
    client.get_profile.side_effect = [deepcopy(source), deepcopy(source)]
    client.prepare_post.return_value = object()
    posted: list[dict] = []

    def assert_boundary(_prepared, payload):
        assert _inside_application_atomic_block() is False
        current = MarketplaceFeedEndpoint.objects.get(pk=endpoint.pk)
        assert current.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
        assert current.profile_revision == endpoint.profile_revision + 1
        posted.append(deepcopy(payload))

    client.post_profile_once.side_effect = assert_boundary
    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 5
    client.post_profile_once.assert_called_once()
    assert len(posted) == 1
    payload = posted[0]
    assert list(payload) == list(source)
    assert payload['autoload_enabled'] is False
    assert payload['report_email'] == 'reports@example.test'
    assert payload['schedule'] == []
    assert payload['feeds_data'][0] == source['feeds_data'][0]
    assert payload['feeds_data'][2] == source['feeds_data'][2]
    assert payload['feeds_data'][1]['feed_name'] == source['feeds_data'][1]['feed_name']
    assert payload['feeds_data'][1]['feed_url'] == marketplace_feed_public_url(endpoint)
    assert 'agreement' not in payload
    assert result.verification_outcome == 'post_submitted_unverified'

    another = _client(source)
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=another,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='not ready'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=4,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )
    another.get_profile.assert_not_called()
    another.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_migrate_normalizes_future_source_marker_before_update_unknown_boundary():
    tenant, account = _account('profile-future-source-marker')
    endpoint = _endpoint(account)
    source = _profile(account)
    future_marker = timezone.now() + timedelta(days=1)
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        profile_verified_at=future_marker,
    )
    endpoint.refresh_from_db()
    client = _client(source)
    client.get_profile.side_effect = [deepcopy(source), deepcopy(source)]
    client.prepare_post.return_value = object()

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_verified_at < endpoint.updated_at
    client.post_profile_once.assert_called_once()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_migrate_refuses_nullable_report_email_before_boundary_or_post():
    tenant, account = _account('profile-null-report-email')
    source = _profile(account, report_email=None)
    endpoint = _endpoint(account, profile_snapshot=source)
    dry_client = _client(source)
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=dry_client,
        ),
        pytest.raises(FeedProfileMigrationSafetyError, match='full upsert'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
        )
    dry_client.prepare_post.assert_not_called()
    dry_client.post_profile_once.assert_not_called()

    client = _client(source)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationSafetyError, match='full upsert'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    assert endpoint.profile_revision == 4
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_migrate_post_failure_stays_update_unknown_and_never_retries():
    tenant, account = _account('profile-post-unknown')
    endpoint = _endpoint(account)
    source = _profile(account)
    client = _client(source)
    client.get_profile.side_effect = [deepcopy(source), deepcopy(source)]
    client.prepare_post.return_value = object()
    client.post_profile_once.side_effect = AvitoProfilePostError('provider echoed secret')

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(
            FeedProfileMigrationProviderUncertain,
            match='GET-only reconciliation',
        ) as caught,
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert client.post_profile_once.call_count == 1
    assert 'secret' not in str(caught.value)


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_identity_change_before_boundary_fences_post():
    tenant, account = _account('profile-identity-fence')
    endpoint = _endpoint(account)
    source = _profile(account)
    client = _client(source)
    client.get_profile.side_effect = [deepcopy(source), deepcopy(source)]

    def mutate_identity():
        MarketplaceAccount.all_objects.filter(pk=account.pk).update(
            external_id='changed-provider-generation',
        )
        return object()

    client.prepare_post.side_effect = mutate_identity
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='identity changed'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_stale_revision_loses_one_shot_post_boundary():
    tenant, account = _account('profile-revision-fence')
    endpoint = _endpoint(account)
    source = _profile(account)
    client = _client(source)
    client.get_profile.side_effect = [deepcopy(source), deepcopy(source)]

    def claim_new_revision():
        MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
            profile_revision=endpoint.profile_revision + 1,
        )
        return object()

    client.prepare_post.side_effect = claim_new_revision
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='revision'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='migrate',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    assert endpoint.profile_revision == 5
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_reconcile_is_get_only_and_verifies_only_exact_target():
    tenant, account = _account('profile-reconcile')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    source_observation = observe_endpoint_profile(endpoint, source)
    target = source_observation.plan.target_profile
    client = _client(target)

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
    assert endpoint.profile_revision == 5
    assert endpoint.profile_fingerprint == source_observation.target_fingerprint
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()
    assert result.verification_outcome == 'target_verified'


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_reconcile_refuses_disabled_serving_generation():
    tenant, account = _account('profile-reconcile-disabled')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    target = observe_endpoint_profile(endpoint, source).plan.target_profile
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        serve_enabled=False,
    )
    client = _client(target)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='generation changed'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.serve_enabled is False
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**ROTATION_SETTINGS)
def test_reconcile_exact_current_target_clears_previous_rotation_key():
    tenant, account = _account('profile-reconcile-rotation')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        token_key_id='profile-v2',
        previous_token_key_id='profile-v1',
    )
    endpoint.refresh_from_db()
    source = _profile(account)
    target = observe_endpoint_profile(endpoint, source).plan.target_profile
    client = _client(target)

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
    assert endpoint.previous_token_key_id == ''
    assert result.verification_outcome == 'target_verified'
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**ROTATION_SETTINGS)
def test_reconcile_previous_key_target_is_drift_and_does_not_clear_rotation():
    tenant, account = _account('profile-reconcile-previous-target')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        token_key_id='profile-v2',
        previous_token_key_id='profile-v1',
    )
    endpoint.refresh_from_db()

    endpoint.token_key_id = 'profile-v1'
    previous_stable_url = marketplace_feed_public_url(endpoint)
    endpoint.refresh_from_db()
    stale_target = _profile(account)
    stale_target['feeds_data'][1]['feed_url'] = previous_stable_url
    client = _client(stale_target)

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationSafetyError),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.previous_token_key_id == 'profile-v1'
    assert endpoint.profile_revision == 4
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**ROTATION_SETTINGS)
def test_reconcile_stale_snapshot_loses_to_signing_key_rotation():
    tenant, account = _account('profile-reconcile-key-race')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    target = observe_endpoint_profile(endpoint, source).plan.target_profile
    client = _client(target)

    def rotate_after_provider_get():
        MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
            token_key_id='profile-v2',
        )
        return deepcopy(target)

    client.get_profile.side_effect = rotate_after_provider_get
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='generation changed'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4
    assert endpoint.token_key_id == 'profile-v2'
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_reconcile_source_and_defensive_drift_never_become_verified():
    tenant, account = _account('profile-reconcile-source')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    client = _client(source)
    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        source_result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )
    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4
    assert source_result.verification_outcome == 'source_confirmed_update_unknown'

    drift = SimpleNamespace(
        outcome='drift',
        source_fingerprint=endpoint.profile_fingerprint,
        target_fingerprint='f' * 64,
        owned_feed_count=1,
        foreign_feed_count=2,
    )
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=_client(source),
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.observe_endpoint_profile',
            return_value=drift,
        ),
        pytest.raises(FeedProfileMigrationSafetyError, match='exact target'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )
    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_resolve_source_requires_two_settled_persisted_observations():
    tenant, account = _account('profile-resolve-source')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    target = observe_endpoint_profile(endpoint, source).plan.target_profile
    boundary_at = timezone.now()
    baseline_at = boundary_at - timedelta(microseconds=1)
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        updated_at=boundary_at,
        profile_verified_at=baseline_at,
    )
    client = _client(source)

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        first_early_at = boundary_at + timedelta(seconds=299)
        with patch(
            'apps.marketplaces.feed_profile_migration.timezone.now',
            return_value=first_early_at,
        ):
            early = run_feed_profile_migration(
                tenant_id=tenant.pk,
                account_id=account.pk,
                phase='resolve-source',
                expected_revision=endpoint.profile_revision,
                expected_source_fingerprint=endpoint.profile_fingerprint,
                apply=True,
            )
        endpoint.refresh_from_db()
        assert endpoint.profile_revision == 4
        assert endpoint.updated_at == boundary_at
        assert endpoint.profile_verified_at == baseline_at
        assert early.verification_outcome == 'source_settlement_pending'
        assert early.settlement_remaining_seconds == 1

        first_due_at = boundary_at + timedelta(seconds=300)
        with patch(
            'apps.marketplaces.feed_profile_migration.timezone.now',
            return_value=first_due_at,
        ):
            first = run_feed_profile_migration(
                tenant_id=tenant.pk,
                account_id=account.pk,
                phase='resolve-source',
                expected_revision=endpoint.profile_revision,
                expected_source_fingerprint=endpoint.profile_fingerprint,
                apply=True,
            )
        endpoint.refresh_from_db()
        assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
        assert endpoint.profile_revision == 5
        assert endpoint.updated_at == boundary_at
        assert endpoint.profile_verified_at == first_due_at
        assert first.verification_outcome == 'source_observation_recorded'
        assert first.settlement_remaining_seconds == 300

        second_early_at = boundary_at + timedelta(seconds=599)
        with patch(
            'apps.marketplaces.feed_profile_migration.timezone.now',
            return_value=second_early_at,
        ):
            resumed_early = run_feed_profile_migration(
                tenant_id=tenant.pk,
                account_id=account.pk,
                phase='resolve-source',
                expected_revision=endpoint.profile_revision,
                expected_source_fingerprint=endpoint.profile_fingerprint,
                apply=True,
            )
        endpoint.refresh_from_db()
        assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
        assert endpoint.profile_revision == 5
        assert endpoint.updated_at == boundary_at
        assert endpoint.profile_verified_at == first_due_at
        assert resumed_early.verification_outcome == 'source_settlement_pending'
        assert resumed_early.settlement_remaining_seconds == 1

        second_due_at = boundary_at + timedelta(seconds=600)
        with patch(
            'apps.marketplaces.feed_profile_migration.timezone.now',
            return_value=second_due_at,
        ):
            resolved = run_feed_profile_migration(
                tenant_id=tenant.pk,
                account_id=account.pk,
                phase='resolve-source',
                expected_revision=endpoint.profile_revision,
                expected_source_fingerprint=endpoint.profile_fingerprint,
                apply=True,
            )

        endpoint.refresh_from_db()
        assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
        assert endpoint.profile_revision == 6
        assert endpoint.previous_token_key_id == ''
        assert resolved.verification_outcome == 'source_resolved'
        assert resolved.settlement_remaining_seconds == 0

        source_bridge = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )
        endpoint.refresh_from_db()
        assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
        assert endpoint.profile_revision == 6
        assert source_bridge.verification_outcome == 'source_confirmed_bridge_ready'

        client.get_profile.return_value = deepcopy(target)
        late_target = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='reconcile',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
    assert endpoint.profile_revision == 7
    assert late_target.verification_outcome == 'target_verified'
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_resolve_source_exact_target_verifies_immediately_without_post():
    tenant, account = _account('profile-resolve-target')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    target = observe_endpoint_profile(endpoint, source).plan.target_profile
    client = _client(target)

    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
        return_value=client,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='resolve-source',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
    assert endpoint.profile_revision == 5
    assert result.verification_outcome == 'target_verified'
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_resolve_source_drift_and_generation_change_never_mutate_state():
    tenant, account = _account('profile-resolve-drift')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    source = _profile(account)
    drift = SimpleNamespace(
        outcome='drift',
        source_fingerprint=endpoint.profile_fingerprint,
        target_fingerprint='f' * 64,
        owned_feed_count=1,
        foreign_feed_count=2,
    )
    drift_client = _client(source)
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=drift_client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.observe_endpoint_profile',
            return_value=drift,
        ),
        pytest.raises(FeedProfileMigrationSafetyError, match='exact profile'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='resolve-source',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )
    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4

    def disable_after_exact_get():
        MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
            serve_enabled=False,
        )
        return deepcopy(source)

    raced_client = _client(source)
    raced_client.get_profile.side_effect = disable_after_exact_get
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=raced_client,
        ),
        pytest.raises(FeedProfileMigrationConflict, match='generation changed'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='resolve-source',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )
    endpoint.refresh_from_db()
    assert endpoint.serve_enabled is False
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4
    raced_client.prepare_post.assert_not_called()
    raced_client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_resolve_source_slow_get_cannot_cross_settlement_threshold():
    tenant, account = _account('profile-resolve-observed-at')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    boundary_at = timezone.now()
    baseline_at = boundary_at - timedelta(microseconds=1)
    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
        updated_at=boundary_at,
        profile_verified_at=baseline_at,
    )
    before_due = boundary_at + timedelta(seconds=299)
    after_due = boundary_at + timedelta(seconds=301)
    client = _client(_profile(account))

    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.timezone.now',
            side_effect=[before_due, after_due],
        ) as clock,
    ):
        result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='resolve-source',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert clock.call_count == 2
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4
    assert endpoint.updated_at == boundary_at
    assert endpoint.profile_verified_at == baseline_at
    assert result.verification_outcome == 'source_settlement_pending'
    assert result.settlement_remaining_seconds == 1

    next_started_at = boundary_at + timedelta(seconds=302)
    next_completed_at = boundary_at + timedelta(seconds=303)
    with (
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.timezone.now',
            side_effect=[next_started_at, next_completed_at],
        ) as next_clock,
    ):
        next_result = run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='resolve-source',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert next_clock.call_count == 2
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 5
    assert endpoint.updated_at == boundary_at
    assert endpoint.profile_verified_at == next_completed_at
    assert next_result.verification_outcome == 'source_observation_recorded'


@pytest.mark.django_db
@pytest.mark.parametrize('invalid_interval', [299, 86_401, True, '300'])
@override_settings(**MIGRATION_SETTINGS)
def test_resolve_source_rejects_invalid_settlement_setting_before_provider_get(
    invalid_interval,
):
    tenant, account = _account(f'profile-resolve-setting-{invalid_interval}')
    endpoint = _endpoint(
        account,
        state=MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    )
    client = _client(_profile(account))

    with (
        override_settings(
            MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS=invalid_interval,
        ),
        patch(
            'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
            return_value=client,
        ),
        pytest.raises(FeedProfileMigrationSafetyError, match='interval is invalid'),
    ):
        run_feed_profile_migration(
            tenant_id=tenant.pk,
            account_id=account.pk,
            phase='resolve-source',
            expected_revision=endpoint.profile_revision,
            expected_source_fingerprint=endpoint.profile_fingerprint,
            apply=True,
        )

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert endpoint.profile_revision == 4
    client.get_profile.assert_not_called()
    client.prepare_post.assert_not_called()
    client.post_profile_once.assert_not_called()


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_status_helper_is_lifecycle_aware_without_exposing_urls():
    _tenant, account = _account('profile-status-helper')
    endpoint = _endpoint(account)
    source = _profile(account)
    source_observation = observe_endpoint_profile(endpoint, source)
    target = source_observation.plan.target_profile

    assert is_profile_feed_configured(endpoint=endpoint, profile=source) is True
    assert is_profile_feed_configured(endpoint=endpoint, profile=target) is False

    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN
    assert is_profile_feed_configured(endpoint=endpoint, profile=source) is True
    assert is_profile_feed_configured(endpoint=endpoint, profile=target) is True

    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.VERIFIED
    endpoint.profile_fingerprint = source_observation.target_fingerprint
    assert is_profile_feed_configured(endpoint=endpoint, profile=target) is True
    assert is_profile_feed_configured(endpoint=endpoint, profile=source) is False

    edited_target = deepcopy(target)
    edited_target['report_email'] = 'new-owner@example.test'
    edited_target['schedule'] = [
        {'rate': 1234, 'weekdays': [1, 3], 'time_slots': [9]},
    ]
    edited_target['feeds_data'][0]['feed_name'] = 'Renamed foreign feed'
    assert (
        is_profile_feed_configured(endpoint=endpoint, profile=edited_target)
        is True
    )

    mixed = deepcopy(edited_target)
    mixed['feeds_data'].append(deepcopy(source['feeds_data'][1]))
    assert is_profile_feed_configured(endpoint=endpoint, profile=mixed) is False

    duplicated = deepcopy(edited_target)
    duplicated['feeds_data'].append(deepcopy(target['feeds_data'][1]))
    assert (
        is_profile_feed_configured(endpoint=endpoint, profile=duplicated)
        is False
    )


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_bridge_parity_rejects_content_type_mismatch():
    _tenant, account = _account('profile-content-type')
    endpoint = _endpoint(account, state=MarketplaceFeedEndpoint.ProfileState.MIGRATING)

    with (
        patch(
            'apps.marketplaces.adapters.avito.profile_migration._feed_digest',
            side_effect=[
                ('a' * 64, 100, 'application/xml'),
                ('a' * 64, 100, 'text/xml'),
                ('a' * 64, 100, 'application/xml'),
            ],
        ),
        patch(
            'apps.marketplaces.adapters.avito.profile_migration._stream_digest',
            return_value=(
                'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
                0,
                {'Location': endpoint.legacy_profile_url},
            ),
        ),
        pytest.raises(AvitoProfileValidationError, match='byte parity'),
    ):
        probe_feed_bridge_parity(endpoint)


@pytest.mark.django_db
@override_settings(**MIGRATION_SETTINGS)
def test_physical_post_has_no_hidden_401_retry_and_keeps_exact_payload():
    _tenant, account = _account('profile-no-retry')
    client = AvitoProfileMigrationClient(account)
    prepared = PreparedAvitoProfilePost('secret-token-must-not-be-repr-visible')
    target = _profile(account)
    response = MagicMock(status_code=401, headers={})

    with (
        patch(
            'apps.marketplaces.adapters.avito.profile_migration._avito_request',
            return_value=response,
        ) as physical,
        patch(
            'apps.marketplaces.adapters.avito.profile_migration.handle_avito_error',
            side_effect=RuntimeError('unauthorized'),
        ),
        pytest.raises(AvitoProfilePostError, match='GET-only reconciliation'),
    ):
        client.post_profile_once(prepared, target)

    physical.assert_called_once()
    assert physical.call_args.kwargs['json'] == target
    assert 'agreement' not in physical.call_args.kwargs['json']
    assert 'secret-token' not in repr(prepared)


@pytest.mark.django_db
def test_invalid_scope_is_rejected_before_client_or_provider_io():
    tenant, account = _account('profile-input-fence')
    with patch(
        'apps.marketplaces.feed_profile_migration.AvitoProfileMigrationClient',
    ) as client_class:
        with pytest.raises(ValueError, match='positive integer'):
            run_feed_profile_migration(
                tenant_id=True,
                account_id=account.pk,
                phase='inspect',
            )
        with pytest.raises(FeedProfileMigrationConflict, match='tenant scope'):
            run_feed_profile_migration(
                tenant_id=tenant.pk + 999,
                account_id=account.pk,
                phase='inspect',
            )
    client_class.assert_not_called()


def test_migration_errors_expose_only_stable_redaction_safe_codes():
    assert FeedProfileMigrationError.code == 'transport_failed'
    assert FeedProfileMigrationConflict.code == 'state_conflict'
    assert FeedProfileMigrationSafetyError.code == 'safety_refused'
    assert (
        FeedProfileMigrationProviderUncertain.code
        == 'provider_outcome_uncertain'
    )
