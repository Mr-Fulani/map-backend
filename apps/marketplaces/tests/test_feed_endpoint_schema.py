import uuid

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.db import (
    DataError,
    IntegrityError,
    connection,
    migrations,
    models,
    transaction,
)
from django.db.migrations.loader import MigrationLoader
from django.db.models.fields import NOT_PROVIDED
from django.test import RequestFactory
from django.utils import timezone

from apps.marketplaces.admin import MarketplaceFeedEndpointAdmin
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.tenants.models import Tenant


PROFILE_STATES = (
    MarketplaceFeedEndpoint.ProfileState.NEW,
    MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
    MarketplaceFeedEndpoint.ProfileState.MIGRATING,
    MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    MarketplaceFeedEndpoint.ProfileState.VERIFIED,
    MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW,
)
STORAGE_MODES = (
    MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
    MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
)
LEGACY_KEY = 'feeds/tenant/avito/account-1/feed.xml'
LEGACY_URL = 'https://storage.example.test/feeds/tenant/avito/account-1/feed.xml'


def _account(*, slug: str) -> MarketplaceAccount:
    tenant = Tenant.objects.create(name=f'Endpoint {slug}', slug=slug)
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Endpoint account {slug}',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-test-credentials',
    )


def _endpoint(account: MarketplaceAccount, **values) -> MarketplaceFeedEndpoint:
    values.setdefault('token_key_id', 'feed-hmac-v1')
    values.setdefault('owner_identity_digest', account_identity_digest(account))
    return MarketplaceFeedEndpoint.objects.create(account=account, **values)


def _constraint(name: str) -> models.BaseConstraint:
    matches = [
        constraint
        for constraint in MarketplaceFeedEndpoint._meta.constraints
        if constraint.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_feed_endpoint_model_is_dark_provider_neutral_bridge_schema():
    assert tuple(value for value, _label in MarketplaceFeedEndpoint.ProfileState.choices) == (
        *PROFILE_STATES,
    )
    assert tuple(value for value, _label in MarketplaceFeedEndpoint.StorageMode.choices) == (
        *STORAGE_MODES,
    )

    public_id = MarketplaceFeedEndpoint._meta.pk
    assert public_id.name == 'public_id'
    assert isinstance(public_id, models.UUIDField)
    assert public_id.default is uuid.uuid4
    assert public_id.editable is False

    account = MarketplaceFeedEndpoint._meta.get_field('account')
    assert isinstance(account, models.OneToOneField)
    assert account.remote_field.on_delete is models.CASCADE
    assert account.remote_field.related_name == 'feed_endpoint'
    assert account.editable is False

    token_key_id = MarketplaceFeedEndpoint._meta.get_field('token_key_id')
    assert token_key_id.max_length == 32
    assert token_key_id.default is NOT_PROVIDED

    previous_key_id = MarketplaceFeedEndpoint._meta.get_field(
        'previous_token_key_id',
    )
    assert previous_key_id.max_length == 32
    assert previous_key_id.blank is True

    owner_digest = MarketplaceFeedEndpoint._meta.get_field(
        'owner_identity_digest',
    )
    assert owner_digest.max_length == 64
    assert owner_digest.default is NOT_PROVIDED

    capability_revision = MarketplaceFeedEndpoint._meta.get_field(
        'capability_revision',
    )
    assert isinstance(capability_revision, models.PositiveBigIntegerField)
    assert capability_revision.default == 1

    serve_enabled = MarketplaceFeedEndpoint._meta.get_field('serve_enabled')
    assert serve_enabled.default is False

    storage_mode = MarketplaceFeedEndpoint._meta.get_field('storage_mode')
    assert storage_mode.default == MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
    assert storage_mode.max_length == 24

    profile_state = MarketplaceFeedEndpoint._meta.get_field('profile_state')
    assert profile_state.default == MarketplaceFeedEndpoint.ProfileState.NEW
    assert profile_state.max_length == 20

    profile_revision = MarketplaceFeedEndpoint._meta.get_field('profile_revision')
    assert isinstance(profile_revision, models.PositiveBigIntegerField)
    assert profile_revision.default == 0

    for name, max_length in (
        ('legacy_object_key', 1024),
        ('legacy_profile_url', 2048),
        ('profile_fingerprint', 64),
    ):
        field = MarketplaceFeedEndpoint._meta.get_field(name)
        assert field.max_length == max_length, name
        assert field.blank is True, name
        assert field.default is NOT_PROVIDED, name

    verified_at = MarketplaceFeedEndpoint._meta.get_field('profile_verified_at')
    assert verified_at.null is True
    assert verified_at.blank is True
    assert verified_at.default is NOT_PROVIDED

    lifecycle_fields = (
        'token_key_id', 'previous_token_key_id', 'owner_identity_digest',
        'capability_revision', 'serve_enabled', 'storage_mode', 'legacy_object_key',
        'legacy_profile_url', 'profile_state', 'profile_fingerprint',
        'profile_revision', 'profile_verified_at', 'current_artifact',
        'artifact_revision', 'artifact_promoted_at',
    )
    for name in lifecycle_fields:
        assert MarketplaceFeedEndpoint._meta.get_field(name).editable is False, name

    field_names = {field.name for field in MarketplaceFeedEndpoint._meta.fields}
    assert {'tenant', 'marketplace', 'token', 'token_digest', 'token_encrypted'}.isdisjoint(
        field_names,
    )
    assert MarketplaceFeedEndpoint._meta.get_field(
        'source_intent_revision',
    ).default == 0

    current_artifact = MarketplaceFeedEndpoint._meta.get_field('current_artifact')
    assert isinstance(current_artifact, models.ForeignKey)
    assert current_artifact.remote_field.on_delete is models.PROTECT
    assert current_artifact.null is True
    assert current_artifact.blank is True
    assert current_artifact.db_index is False

    artifact_revision = MarketplaceFeedEndpoint._meta.get_field('artifact_revision')
    assert isinstance(artifact_revision, models.PositiveBigIntegerField)
    assert artifact_revision.default == 0

    artifact_promoted_at = MarketplaceFeedEndpoint._meta.get_field(
        'artifact_promoted_at',
    )
    assert artifact_promoted_at.null is True
    assert artifact_promoted_at.blank is True
    assert artifact_promoted_at.default is NOT_PROVIDED


def test_feed_endpoint_constraints_and_index_are_named_and_bounded():
    expected_constraints = {
        'mkt_feed_ep_key_id_format',
        'mkt_feed_ep_prev_key',
        'mkt_feed_ep_prev_key_state',
        'mkt_feed_ep_owner_digest',
        'mkt_feed_ep_cap_revision',
        'mkt_feed_ep_storage_mode',
        'mkt_feed_ep_profile_state',
        'mkt_feed_ep_legacy_bundle',
        'mkt_feed_ep_state_legacy',
        'mkt_feed_ep_profile_baseline',
        'mkt_feed_ep_servable_baseline',
        'mkt_feed_ep_serve_guard',
        'mkt_feed_ep_art_bundle',
    }
    assert {
        constraint.name for constraint in MarketplaceFeedEndpoint._meta.constraints
    } == expected_constraints
    assert all(
        isinstance(_constraint(name), models.CheckConstraint)
        for name in expected_constraints
    )

    indexes = {index.name: index for index in MarketplaceFeedEndpoint._meta.indexes}
    assert set(indexes) == {
        'mkt_feed_ep_state_updated',
        'mkt_feed_ep_current_art',
    }
    assert indexes['mkt_feed_ep_state_updated'].fields == [
        'profile_state', 'updated_at', 'public_id',
    ]
    current_artifact_index = indexes['mkt_feed_ep_current_art']
    assert current_artifact_index.fields == ['current_artifact', 'public_id']
    assert current_artifact_index.condition == models.Q(
        current_artifact__isnull=False,
    )

    for named_object in (
        *MarketplaceFeedEndpoint._meta.constraints,
        *indexes.values(),
    ):
        assert len(named_object.name) <= 30
        predicate = getattr(named_object, 'condition', None)
        if predicate is not None:
            assert 'now(' not in str(predicate).lower().replace(' ', '')


@pytest.mark.django_db
def test_feed_endpoint_database_enforces_one_stable_identity_per_account():
    account = _account(slug='endpoint-account-unique')
    _endpoint(account)

    with pytest.raises(IntegrityError), transaction.atomic():
        _endpoint(account, public_id=uuid.uuid4())

    other_account = _account(slug='endpoint-public-id-unique')
    public_id = uuid.uuid4()
    MarketplaceFeedEndpoint.objects.create(
        public_id=public_id,
        account=other_account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=account_identity_digest(other_account),
    )
    third_account = _account(slug='endpoint-public-id-conflict')
    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedEndpoint.objects.create(
            public_id=public_id,
            account=third_account,
            token_key_id='feed-hmac-v1',
            owner_identity_digest=account_identity_digest(third_account),
        )


@pytest.mark.django_db
def test_feed_endpoint_database_rejects_invalid_bridge_and_profile_bundles():
    now = timezone.now()
    invalid_values = (
        {'token_key_id': ''},
        {'token_key_id': ' invalid'},
        {'token_key_id': 'x' * 33},
        {'owner_identity_digest': 'not-a-digest'},
        {'capability_revision': 0},
        {'previous_token_key_id': 'feed-hmac-v1'},
        {'previous_token_key_id': ' invalid'},
        {'previous_token_key_id': 'feed-hmac-v0'},
        {'storage_mode': 'unsupported'},
        {'profile_state': 'unsupported'},
        {'legacy_object_key': LEGACY_KEY},
        {'legacy_profile_url': LEGACY_URL},
        {'legacy_object_key': LEGACY_KEY, 'legacy_profile_url': 'http://feed.test/feed.xml'},
        {'profile_state': MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY},
        {
            'profile_state': MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
        },
        {
            'profile_fingerprint': 'a' * 64,
            'profile_verified_at': None,
        },
        {'profile_verified_at': now},
        {
            'profile_fingerprint': 'not-a-sha256',
            'profile_verified_at': now,
        },
        {
            'profile_state': MarketplaceFeedEndpoint.ProfileState.VERIFIED,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
        },
        {
            'serve_enabled': True,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
        },
        {
            'storage_mode': MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
            'profile_state': MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            'serve_enabled': True,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
        },
    )

    for position, values in enumerate(invalid_values):
        account = _account(slug=f'endpoint-invalid-{position}')
        # CHECK violations are IntegrityError; an overlong varchar is rejected
        # earlier by PostgreSQL as DataError. Both prove the database boundary
        # fails closed without relying on model-level validation.
        with pytest.raises((IntegrityError, DataError)), transaction.atomic():
            _endpoint(account, **values)


@pytest.mark.django_db
def test_feed_endpoint_database_allows_recoverable_operational_states():
    now = timezone.now()
    valid_values = (
        {},
        {'profile_state': MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW},
        {
            'profile_state': MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
            'profile_fingerprint': 'a' * 64,
            'profile_verified_at': now,
            'serve_enabled': True,
        },
        {
            'profile_state': MarketplaceFeedEndpoint.ProfileState.MIGRATING,
            'previous_token_key_id': 'feed-hmac-v0',
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
            'profile_fingerprint': 'a' * 64,
            'profile_verified_at': now,
        },
        {
            'profile_state': MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
            'profile_fingerprint': 'a' * 64,
            'profile_verified_at': now,
        },
        {
            'profile_state': MarketplaceFeedEndpoint.ProfileState.VERIFIED,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
            'profile_fingerprint': 'b' * 64,
            'profile_verified_at': now,
        },
        {
            'storage_mode': MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
            'profile_state': MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            'legacy_object_key': LEGACY_KEY,
            'legacy_profile_url': LEGACY_URL,
            'profile_fingerprint': 'c' * 64,
            'profile_verified_at': now,
        },
    )

    endpoints = [
        _endpoint(_account(slug=f'endpoint-valid-{position}'), **values)
        for position, values in enumerate(valid_values)
    ]

    assert len(endpoints) == len(valid_values)
    assert endpoints[0].profile_state == MarketplaceFeedEndpoint.ProfileState.NEW
    assert endpoints[1].legacy_profile_url == ''
    assert endpoints[2].serve_enabled is True
    assert endpoints[3].previous_token_key_id == 'feed-hmac-v0'
    assert endpoints[-1].serve_enabled is False


@pytest.mark.django_db
def test_feed_endpoint_survives_soft_delete_but_follows_authorized_hard_purge():
    account = _account(slug='endpoint-account-lifecycle')
    endpoint = _endpoint(account)

    account.soft_delete()

    assert MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).exists()
    account.hard_delete()
    assert not MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).exists()


@pytest.mark.django_db
def test_feed_endpoint_admin_is_diagnostics_only():
    superuser = get_user_model().objects.create_superuser(
        'feed-endpoint-admin@example.com',
        'pass12345',
    )
    request = RequestFactory().get('/admin/marketplaces/marketplacefeedendpoint/')
    request.user = superuser
    model_admin = MarketplaceFeedEndpointAdmin(
        MarketplaceFeedEndpoint,
        AdminSite(),
    )

    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_change_permission(
        request,
        obj=MarketplaceFeedEndpoint(),
    ) is False
    assert model_admin.has_delete_permission(request) is False
    assert model_admin.has_delete_permission(
        request,
        obj=MarketplaceFeedEndpoint(),
    ) is False
    assert 'delete_selected' not in model_admin.get_actions(request)
    assert set(model_admin.readonly_fields) == {
        field.name for field in MarketplaceFeedEndpoint._meta.fields
    }


@pytest.mark.django_db
def test_feed_endpoint_migration_is_additive_dark_schema_only():
    loader = MigrationLoader(connection)
    matches = [
        migration
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == 'marketplaces' and name.startswith('0025_')
    ]
    assert len(matches) == 1
    migration = matches[0]
    assert migration.atomic is True
    assert migration.dependencies == [
        ('marketplaces', '0024_feed_run_listing_concurrent_index'),
    ]
    assert [type(operation) for operation in migration.operations] == [
        migrations.CreateModel,
    ]

    create = migration.operations[0]
    assert create.name == 'MarketplaceFeedEndpoint'
    fields = dict(create.fields)
    assert set(fields) == {
        'public_id', 'created_at', 'updated_at', 'account', 'token_key_id',
        'previous_token_key_id', 'owner_identity_digest', 'capability_revision',
        'serve_enabled', 'storage_mode', 'legacy_object_key',
        'legacy_profile_url', 'profile_state', 'profile_fingerprint',
        'profile_revision', 'profile_verified_at',
    }
    assert isinstance(fields['public_id'], models.UUIDField)
    assert fields['public_id'].primary_key is True
    assert isinstance(fields['account'], models.OneToOneField)
    assert fields['account'].remote_field.on_delete is models.CASCADE
    assert fields['token_key_id'].default is NOT_PROVIDED
    assert fields['serve_enabled'].default is False
    assert fields['storage_mode'].default == 'legacy_bridge'
    assert fields['profile_state'].default == 'new'
    assert fields['legacy_object_key'].blank is True
    assert fields['legacy_profile_url'].blank is True
    assert fields['profile_verified_at'].null is True

    assert [index.name for index in create.options['indexes']] == [
        'mkt_feed_ep_state_updated',
    ]
    assert {
        constraint.name for constraint in create.options['constraints']
    } == {
        'mkt_feed_ep_key_id_format',
        'mkt_feed_ep_prev_key',
        'mkt_feed_ep_prev_key_state',
        'mkt_feed_ep_owner_digest',
        'mkt_feed_ep_cap_revision',
        'mkt_feed_ep_storage_mode',
        'mkt_feed_ep_profile_state',
        'mkt_feed_ep_legacy_bundle',
        'mkt_feed_ep_state_legacy',
        'mkt_feed_ep_profile_baseline',
        'mkt_feed_ep_servable_baseline',
        'mkt_feed_ep_serve_guard',
        'mkt_feed_ep_private_dark',
    }
    assert not any(
        isinstance(operation, (migrations.RunPython, migrations.RunSQL))
        for operation in migration.operations
    )
