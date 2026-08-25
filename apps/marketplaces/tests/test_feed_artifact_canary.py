import base64
import io
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.marketplaces.adapters.avito.feed_builder import build_stop_feed
from apps.marketplaces.feed_artifact_canary import (
    activate_private_feed_canary,
    inspect_private_feed_canary,
    rollback_private_feed_canary,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


class _ExactVersionClient:
    put_total_max_attempts = 1

    def __init__(self):
        self.body = None
        self.request = None

    def put_object_once(self, **kwargs):
        self.request = kwargs.copy()
        self.body = kwargs['Body'].read()
        return {
            'VersionId': 'canary-version-1',
            'ChecksumSHA256': kwargs['ChecksumSHA256'],
        }

    def _response(self, *, include_body=False):
        response = {
            'VersionId': 'canary-version-1',
            'ContentLength': len(self.body),
            'ContentType': 'application/xml',
            'Metadata': self.request['Metadata'].copy(),
            'ChecksumSHA256': base64.b64encode(
                bytes.fromhex(self.request['Metadata']['payload-sha256']),
            ).decode('ascii'),
        }
        if include_body:
            response['Body'] = io.BytesIO(self.body)
        return response

    def head_object(self, **kwargs):
        return self._response()

    def get_object(self, **kwargs):
        return self._response(include_body=True)


def _legacy_endpoint(slug='p6-canary'):
    tenant = Tenant.objects.create(name='P6 Canary', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='P6 Canary Avito',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-test-credentials',
        feed_intent_revision=1,
        feed_intent_dispatched_revision=1,
    )
    owner_digest = account_identity_digest(account)
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=owner_digest,
        serve_enabled=True,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        legacy_object_key=f'feeds/{slug}/feed.xml',
        legacy_profile_url=f'https://legacy.example.test/{slug}/feed.xml',
        profile_state=MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        profile_fingerprint='a' * 64,
        profile_revision=1,
        profile_verified_at=timezone.now(),
        source_intent_revision=1,
    )
    return account, endpoint


def _canary_settings(settings):
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'canary'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'private_generation'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED = False
    settings.MARKETPLACE_FEED_ARTIFACT_BUCKET = 'private-feed-artifacts'
    settings.MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER = 'folder-123'
    settings.MARKETPLACE_FEED_ARTIFACT_MAX_BYTES = 1024 * 1024


def test_zero_listing_canary_uses_stop_then_exact_rollback(settings):
    _canary_settings(settings)
    account, original_endpoint = _legacy_endpoint()
    client = _ExactVersionClient()

    inspection = inspect_private_feed_canary(account.pk)
    assert inspection.listing_count == 0
    assert inspection.runtime_ready is True

    with (
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ),
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_object_client',
            return_value=client,
        ),
    ):
        result = activate_private_feed_canary(account.pk)

    assert client.body == build_stop_feed()
    assert result.listing_count == 0
    endpoint = MarketplaceFeedEndpoint.objects.get(pk=original_endpoint.pk)
    run = MarketplaceFeedRun.objects.get(pk=result.run_id)
    artifact = MarketplaceFeedArtifact.objects.get(pk=result.artifact_id)
    assert endpoint.storage_mode == 'private_generation'
    assert endpoint.serve_enabled is True
    assert endpoint.current_artifact_id == artifact.pk
    assert endpoint.artifact_revision == 1
    assert run.state == MarketplaceFeedRun.State.SUCCEEDED
    assert run.feed_artifact_id == artifact.pk

    rollback = rollback_private_feed_canary(
        account.pk,
        expected_artifact_id=artifact.pk,
        expected_artifact_revision=endpoint.artifact_revision,
    )
    assert rollback.storage_mode == 'legacy_bridge'
    endpoint.refresh_from_db()
    assert endpoint.storage_mode == 'legacy_bridge'
    assert endpoint.current_artifact_id == artifact.pk
    assert MarketplaceFeedArtifact.objects.filter(pk=artifact.pk).exists()

    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(
            storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        )
