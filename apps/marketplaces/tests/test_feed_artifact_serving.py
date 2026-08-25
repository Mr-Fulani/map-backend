import copy
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import DatabaseError, transaction
from django.test import Client, override_settings
from django.utils import timezone

from apps.marketplaces.feed_artifact_serving import (
    PrivateFeedNotServable,
    _LockedPrivateFeedSnapshot,
    _presigned_exact_version_location,
    _validate_locked_private_snapshot,
    issue_private_feed_redirect,
    private_feed_route_enabled,
)
from apps.marketplaces.feed_endpoint import marketplace_feed_capability
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.tenants.models import Tenant


SIGNING_KEY = b'private-serving-test-key-material-32b'
SERVING_SETTINGS = {
    'MARKETPLACE_FEED_ARTIFACT_MODE': 'canary',
    'MARKETPLACE_FEED_ARTIFACT_BUCKET': 'private-feed-artifacts',
    'MARKETPLACE_FEED_REDIRECT_TTL_SECONDS': 60,
    'MARKETPLACE_FEED_URL_SIGNING_KEYS': {'feed-v1': SIGNING_KEY},
}


def _snapshot():
    endpoint_id = uuid.uuid4()
    run_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    tenant = SimpleNamespace(pk=71, id=71, is_active=True)
    account = SimpleNamespace(
        pk=81,
        id=81,
        tenant=tenant,
        tenant_id=tenant.pk,
        deleted_at=None,
        is_active=True,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        feed_intent_revision=3,
    )
    endpoint = SimpleNamespace(
        pk=endpoint_id,
        public_id=endpoint_id,
        account=account,
        account_id=account.pk,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=True,
        profile_state=MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        current_artifact_id=artifact_id,
        artifact_promoted_at=timezone.now(),
        source_intent_revision=3,
        artifact_revision=4,
        owner_identity_digest='a' * 64,
        token_key_id='feed-v1',
        previous_token_key_id='',
        capability_revision=5,
    )
    run = SimpleNamespace(
        pk=run_id,
        id=run_id,
        account_id=account.pk,
        tenant_id=tenant.pk,
        marketplace=account.marketplace,
        feed_artifact_id=artifact_id,
        source_intent_revision=2,
        endpoint_revision=3,
        artifact_upload_attempt=1,
        predecessor_artifact_id=uuid.uuid4(),
        payload_sha256='b' * 64,
        account_identity_digest='a' * 64,
        revision=9,
    )
    artifact = SimpleNamespace(
        pk=artifact_id,
        id=artifact_id,
        endpoint_id=endpoint_id,
        account_id=account.pk,
        run_id=run_id,
        upload_attempt=1,
        storage_bucket='private-feed-artifacts',
        object_key=(
            f'private-feeds/v1/{endpoint_id}/{run_id}/00001/feed.xml'
        ),
        object_version_id='exact-version-id',
        payload_sha256='b' * 64,
        content_type='application/xml',
        verification_method='version_readback_sha256',
    )
    return _LockedPrivateFeedSnapshot(
        account=account,
        endpoint=endpoint,
        artifact=artifact,
        run=run,
    )


@override_settings(**SERVING_SETTINGS)
def test_locked_snapshot_accepts_current_artifact_while_newer_source_is_building():
    snapshot = _snapshot()

    with patch(
        'apps.marketplaces.feed_artifact_serving.account_identity_digest',
        return_value='a' * 64,
    ), patch(
        'apps.marketplaces.feed_artifact_serving.'
        'accepted_marketplace_feed_capability_key_id',
        return_value='feed-v1',
    ):
        accepted = _validate_locked_private_snapshot(
            snapshot,
            provided_capability='never-persist-this-capability',
        )

    assert accepted == 'feed-v1'
    assert snapshot.run.source_intent_revision == 2
    assert snapshot.endpoint.source_intent_revision == 3


@override_settings(**SERVING_SETTINGS)
@pytest.mark.parametrize(
    ('target', 'field', 'value'),
    (
        ('endpoint', 'current_artifact_id', uuid.uuid4()),
        ('endpoint', 'artifact_revision', 5),
        ('endpoint', 'owner_identity_digest', 'c' * 64),
        ('account.tenant', 'is_active', False),
        ('artifact', 'account_id', 999),
        ('artifact', 'object_key', 'private-feeds/v1/stale/feed.xml'),
        ('run', 'tenant_id', 999),
        ('run', 'endpoint_revision', 2),
    ),
)
def test_locked_snapshot_rejects_stale_or_cross_owner_coordinates(
    target,
    field,
    value,
):
    snapshot = copy.deepcopy(_snapshot())
    instance = snapshot
    for part in target.split('.'):
        instance = getattr(instance, part)
    setattr(instance, field, value)

    with patch(
        'apps.marketplaces.feed_artifact_serving.account_identity_digest',
        return_value='a' * 64,
    ), patch(
        'apps.marketplaces.feed_artifact_serving.'
        'accepted_marketplace_feed_capability_key_id',
        return_value='feed-v1',
    ), pytest.raises(PrivateFeedNotServable):
        _validate_locked_private_snapshot(
            snapshot,
            provided_capability='never-persist-this-capability',
        )


@override_settings(**SERVING_SETTINGS)
@pytest.mark.parametrize('method', ('GET', 'HEAD'))
def test_presigner_is_method_bound_and_pins_exact_bucket_key_and_version(
    method,
):
    snapshot = _snapshot()
    presigner = Mock(return_value=(
        'https://storage.yandexcloud.net/private-feed-artifacts/'
        f'{snapshot.artifact.object_key}'
        '?versionId=exact-version-id&X-Amz-Expires=60&X-Amz-Signature=opaque'
    ))

    location = _presigned_exact_version_location(
        snapshot,
        request_method=method,
        ttl_seconds=60,
        presign_exact_version=presigner,
    )

    assert location.startswith('https://storage.yandexcloud.net/')
    presigner.assert_called_once_with(
        bucket=snapshot.artifact.storage_bucket,
        object_key=snapshot.artifact.object_key,
        object_version_id=snapshot.artifact.object_version_id,
        request_method=method,
        expires_in=60,
    )


@pytest.mark.django_db
@override_settings(**SERVING_SETTINGS)
def test_issue_redirect_persists_only_bounded_evidence_before_returning_location():
    snapshot = _snapshot()
    location = (
        'https://storage.yandexcloud.net/private-feed-artifacts/feed.xml'
        '?X-Amz-Signature=opaque'
    )
    evidence_manager = Mock()
    sensitive_capability = 'sensitive-capability-must-not-be-stored'
    presigner = Mock()

    with patch(
        'apps.marketplaces.feed_artifact_serving._lock_private_feed_snapshot',
        return_value=snapshot,
    ), patch(
        'apps.marketplaces.feed_artifact_serving._validate_locked_private_snapshot',
        return_value='feed-v1',
    ), patch(
        'apps.marketplaces.feed_artifact_serving._presigned_exact_version_location',
        return_value=location,
    ), patch(
        'apps.marketplaces.feed_artifact_serving.'
        'MarketplaceFeedFetchEvidence.objects',
        evidence_manager,
    ):
        redirect = issue_private_feed_redirect(
            public_id=snapshot.endpoint.pk,
            provided_capability=sensitive_capability,
            request_method='GET',
            presign_exact_version=presigner,
        )

    assert redirect.location == location
    evidence_manager.create.assert_called_once()
    evidence = evidence_manager.create.call_args.kwargs
    assert evidence == {
        'endpoint': snapshot.endpoint,
        'artifact': snapshot.artifact,
        'request_method': 'GET',
        'accepted_token_key_id': 'feed-v1',
        'capability_revision': snapshot.endpoint.capability_revision,
        'endpoint_revision': snapshot.endpoint.artifact_revision,
        'source_intent_revision': snapshot.run.source_intent_revision,
        'run_revision': snapshot.run.revision,
        'redirect_expires_at': redirect.expires_at,
    }
    assert sensitive_capability not in repr(evidence)
    assert location not in repr(evidence)
    for prohibited in (
        'raw_capability', 'query_string', 'header', 'ip_address',
        'user_agent', 'location',
    ):
        assert all(prohibited not in field.lower() for field in evidence)


@pytest.mark.django_db
@override_settings(**SERVING_SETTINGS)
def test_evidence_database_failure_returns_no_redirect_value():
    snapshot = _snapshot()
    presigner = Mock()
    with patch(
        'apps.marketplaces.feed_artifact_serving._lock_private_feed_snapshot',
        return_value=snapshot,
    ), patch(
        'apps.marketplaces.feed_artifact_serving._validate_locked_private_snapshot',
        return_value='feed-v1',
    ), patch(
        'apps.marketplaces.feed_artifact_serving._presigned_exact_version_location',
        return_value=(
            'https://storage.yandexcloud.net/private-feed-artifacts/feed.xml'
            '?X-Amz-Signature=opaque'
        ),
    ), patch(
        'apps.marketplaces.feed_artifact_serving.'
        'MarketplaceFeedFetchEvidence.objects.create',
        side_effect=DatabaseError('evidence unavailable'),
    ), pytest.raises(DatabaseError):
        issue_private_feed_redirect(
            public_id=snapshot.endpoint.pk,
            provided_capability='sensitive-capability',
            request_method='GET',
            presign_exact_version=presigner,
        )


@pytest.mark.django_db(transaction=True)
@override_settings(**SERVING_SETTINGS)
def test_redirect_boundary_rejects_a_caller_outer_transaction():
    """A Location cannot escape while its evidence is only a savepoint."""

    snapshot = _snapshot()
    presigner = Mock()
    with patch(
        'apps.marketplaces.feed_artifact_serving._lock_private_feed_snapshot',
        return_value=snapshot,
    ) as lock_snapshot, transaction.atomic(), pytest.raises(RuntimeError):
        issue_private_feed_redirect(
            public_id=snapshot.endpoint.pk,
            provided_capability='sensitive-capability',
            request_method='GET',
            presign_exact_version=presigner,
        )

    lock_snapshot.assert_not_called()
    presigner.assert_not_called()


@pytest.mark.django_db
@override_settings(**SERVING_SETTINGS)
def test_current_database_serve_constraint_keeps_private_route_dark():
    tenant = Tenant.objects.create(name='Private route dark', slug='private-route-dark')
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Private route dark',
        external_id='private-route-dark',
        credentials_enc=b'opaque-private-route-dark',
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-v1',
        owner_identity_digest=account_identity_digest(account),
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=False,
    )
    capability = marketplace_feed_capability(endpoint)

    assert private_feed_route_enabled(endpoint) is False
    response = Client().get(
        '/marketplace-feeds/v1/feed.xml',
        {'id': str(endpoint.pk), 'key': capability},
    )

    assert response.status_code == 404
    assert 'Location' not in response


@pytest.mark.django_db
@override_settings(**SERVING_SETTINGS)
def test_route_emits_no_location_when_evidence_transaction_fails():
    tenant = Tenant.objects.create(
        name='Private evidence failure',
        slug='private-evidence-failure',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Private evidence failure',
        external_id='private-evidence-failure',
        credentials_enc=b'opaque-private-evidence-failure',
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-v1',
        owner_identity_digest=account_identity_digest(account),
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=False,
    )
    capability = marketplace_feed_capability(endpoint)

    with patch(
        'apps.marketplaces.feed_artifact_serving.private_feed_route_enabled',
        return_value=True,
    ), patch(
        'apps.marketplaces.feed_artifact_serving.issue_private_feed_redirect',
        side_effect=DatabaseError('evidence unavailable'),
    ):
        response = Client().get(
            '/marketplace-feeds/v1/feed.xml',
            {'id': str(endpoint.pk), 'key': capability},
        )

    assert response.status_code == 404
    assert 'Location' not in response


@pytest.mark.django_db
@override_settings(**SERVING_SETTINGS)
def test_private_route_injects_dedicated_exact_version_presigner():
    tenant = Tenant.objects.create(
        name='Private route presigner',
        slug='private-route-presigner',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Private route presigner',
        external_id='private-route-presigner',
        credentials_enc=b'opaque-private-route-presigner',
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-v1',
        owner_identity_digest=account_identity_digest(account),
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=False,
    )
    capability = marketplace_feed_capability(endpoint)
    private_redirect = SimpleNamespace(
        location=(
            'https://storage.yandexcloud.net/private-feed-artifacts/feed.xml'
            '?versionId=exact-version&X-Amz-Signature=opaque'
        ),
    )

    with patch(
        'apps.marketplaces.feed_artifact_serving.private_feed_route_enabled',
        return_value=True,
    ), patch(
        'apps.marketplaces.feed_artifact_serving.issue_private_feed_redirect',
        return_value=private_redirect,
    ) as issue_redirect:
        response = Client().get(
            '/marketplace-feeds/v1/feed.xml',
            {'id': str(endpoint.pk), 'key': capability},
        )

    assert response.status_code == 307
    assert response['Location'] == private_redirect.location
    call = issue_redirect.call_args.kwargs
    assert call['public_id'] == endpoint.pk
    assert call['request_method'] == 'GET'
    assert call['provided_capability'] == capability
    assert call['presign_exact_version'].__name__ == (
        'presign_private_feed_exact_version'
    )
