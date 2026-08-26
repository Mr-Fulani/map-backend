import base64
import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.marketplaces.adapters.avito.feed_builder import build_stop_feed
from apps.marketplaces.feed_artifact_canary import (
    PrivateFeedCanaryError,
    activate_private_feed_canary,
    inspect_private_feed_canary,
    resume_private_feed_canary,
    rollback_private_feed_canary,
)
from apps.marketplaces.feed_artifact_put_reconciliation import (
    PutOriginTerminationAttestation,
    PutPendingAttemptReference,
    reconcile_put_pending_upload_attempt,
)
from apps.marketplaces.feed_artifact_storage import (
    FeedArtifactUploadOutcomeUnknown,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedPutReconciliationAudit,
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


class _UnknownPutClient(_ExactVersionClient):
    def put_object_once(self, **kwargs):
        self.request = kwargs.copy()
        self.body = kwargs['Body'].read()
        return {'ChecksumSHA256': kwargs['ChecksumSHA256']}


class _AuthoritativeEmptyVersionClient:
    authoritative_exact_key_version_listing = True
    adapter_policy_revision = 'yandex-list-versions-v1'
    canary_policy_revision = 'account4-empty-list-2026-08-26-v1'

    def list_object_versions(self, **kwargs):
        return {
            'Name': kwargs['Bucket'],
            'Prefix': kwargs['Prefix'],
            'MaxKeys': kwargs['MaxKeys'],
            'Versions': [],
            'DeleteMarkers': [],
            'IsTruncated': False,
        }


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


def _reconcile_unknown_attempt(attempt):
    put_started_at = timezone.now() - timedelta(minutes=20)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = 'replica'")
        cursor.execute(
            '''
            UPDATE marketplaces_marketplacefeedartifactuploadattempt
               SET created_at = %s,
                   updated_at = %s,
                   put_started_at = %s
             WHERE id = %s
            ''',
            [
                put_started_at - timedelta(seconds=1),
                put_started_at,
                put_started_at,
                attempt.pk,
            ],
        )
        assert cursor.rowcount == 1
    attempt.refresh_from_db()
    termination = PutOriginTerminationAttestation(
        evidence_reference='container-recreated-after-put',
        evidence_digest='a' * 64,
        operator_identity_digest='b' * 64,
        origin_process_identity_digest='c' * 64,
        digest_scheme_revision='hmac-sha256-v1',
        identity_digest_key_revision='django-secret-key-2026-08',
        origin_process_id=2_000_000_000,
        origin_process_terminated_at=(
            attempt.put_started_at + timedelta(seconds=1)
        ),
        operator_confirmed=True,
    )
    reference = PutPendingAttemptReference(
        tenant_id=attempt.run.tenant_id,
        account_id=attempt.account_id,
        endpoint_id=attempt.endpoint_id,
        run_id=attempt.run_id,
        attempt_id=attempt.pk,
        expected_revision=attempt.revision,
    )
    return reconcile_put_pending_upload_attempt(
        reference,
        client=_AuthoritativeEmptyVersionClient(),
        termination=termination,
    )


@pytest.mark.django_db(transaction=True)
def test_audited_no_object_canary_resumes_with_new_immutable_attempt(settings):
    _canary_settings(settings)
    account, endpoint = _legacy_endpoint('p6-resume')
    unknown_client = _UnknownPutClient()
    with (
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ),
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_object_client',
            return_value=unknown_client,
        ),
        pytest.raises(FeedArtifactUploadOutcomeUnknown),
    ):
        activate_private_feed_canary(account.pk)

    run = MarketplaceFeedRun.objects.get(account=account)
    first_attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert run.revision == 1
    assert first_attempt.state == 'put_pending'
    MarketplaceFeedRun.objects.filter(pk=run.pk).update(
        claimed_until=timezone.now() - timedelta(seconds=1),
    )

    reconciliation = _reconcile_unknown_attempt(first_attempt)
    assert reconciliation.outcome == 'no_object_by_reviewed_settlement_policy'
    assert reconciliation.revision == 2
    assert MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=first_attempt,
    ).count() == 1

    retry_client = _ExactVersionClient()
    with (
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ),
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_object_client',
            return_value=retry_client,
        ),
    ):
        result = resume_private_feed_canary(
            account.pk,
            expected_run_id=run.pk,
            expected_run_revision=run.revision,
            expected_attempt_id=first_attempt.pk,
            expected_attempt_revision=reconciliation.revision,
        )

    attempts = list(
        MarketplaceFeedArtifactUploadAttempt.objects.filter(run=run).order_by(
            'attempt_no',
        ),
    )
    assert [attempt.state for attempt in attempts] == ['no_object', 'attached']
    assert attempts[0].object_key.endswith('/00001/feed.xml')
    assert attempts[1].object_key.endswith('/00002/feed.xml')
    assert attempts[0].object_key != attempts[1].object_key
    assert result.artifact_id == MarketplaceFeedArtifact.objects.get(
        run=run,
    ).pk
    endpoint.refresh_from_db()
    assert endpoint.storage_mode == 'private_generation'


@pytest.mark.django_db(transaction=True)
def test_resume_refuses_stale_reconciliation_fence_before_second_put(settings):
    _canary_settings(settings)
    account, _ = _legacy_endpoint('p6-resume-stale')
    unknown_client = _UnknownPutClient()
    with (
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ),
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_object_client',
            return_value=unknown_client,
        ),
        pytest.raises(FeedArtifactUploadOutcomeUnknown),
    ):
        activate_private_feed_canary(account.pk)
    run = MarketplaceFeedRun.objects.get(account=account)
    first_attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    MarketplaceFeedRun.objects.filter(pk=run.pk).update(
        claimed_until=timezone.now() - timedelta(seconds=1),
    )
    reconciliation = _reconcile_unknown_attempt(first_attempt)
    retry_client = _ExactVersionClient()

    with (
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ),
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_object_client',
            return_value=retry_client,
        ),
        pytest.raises(PrivateFeedCanaryError),
    ):
        resume_private_feed_canary(
            account.pk,
            expected_run_id=run.pk,
            expected_run_revision=run.revision,
            expected_attempt_id=first_attempt.pk,
            expected_attempt_revision=reconciliation.revision + 1,
        )

    assert retry_client.request is None
    assert MarketplaceFeedArtifactUploadAttempt.objects.filter(run=run).count() == 1
