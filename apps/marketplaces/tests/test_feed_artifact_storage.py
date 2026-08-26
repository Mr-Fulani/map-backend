import base64
import hashlib
import io
import tempfile
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.db.models import F
from django.test import override_settings
from django.utils import timezone

from apps.marketplaces import feed_artifact_storage as storage_module
from apps.marketplaces.feed_artifact_storage import (
    FeedArtifactAttemptBlocked,
    FeedArtifactConfigurationError,
    FeedArtifactContentError,
    FeedArtifactResumeRequired,
    FeedArtifactUploadOutcomeUnknown,
    FeedArtifactVerificationError,
    OrphanedFeedArtifactUpload,
    PrivateFeedArtifactStorageService,
    StaleFeedArtifactClaim,
)
from apps.marketplaces.feed_workflow import (
    account_identity_digest,
    claim_due_run_for_account,
)
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


@pytest.fixture(autouse=True)
def _shadow_artifact_mode(settings):
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'shadow'


class FakePrivateS3Client:
    put_total_max_attempts = 1

    def __init__(
        self,
        *,
        version_id='artifact-version-1',
        put_checksum=True,
        head_checksum=True,
        readback=None,
        after_get=None,
        after_put=None,
    ):
        self.version_id = version_id
        self.put_checksum = put_checksum
        self.head_checksum = head_checksum
        self.readback = readback
        self.after_get = after_get
        self.after_put = after_put
        self.calls = []
        self.upload = None

    def put_object_once(self, **kwargs):
        self.calls.append(('put_object_once', kwargs.copy()))
        body = kwargs['Body'].read()
        self.upload = {
            'body': body,
            'checksum': kwargs['ChecksumSHA256'],
            'content_type': kwargs['ContentType'],
            'metadata': kwargs['Metadata'].copy(),
        }
        response = {}
        if self.version_id is not None:
            response['VersionId'] = self.version_id
        if self.put_checksum:
            response['ChecksumSHA256'] = kwargs['ChecksumSHA256']
        if self.after_put is not None:
            self.after_put()
        return response

    def _read_response(self, *, body=False):
        response = {
            'VersionId': self.version_id,
            'ContentLength': len(self.upload['body']),
            'ContentType': self.upload['content_type'],
            'Metadata': self.upload['metadata'].copy(),
        }
        if self.head_checksum:
            response['ChecksumSHA256'] = self.upload['checksum']
        if body:
            response['Body'] = io.BytesIO(
                self.upload['body'] if self.readback is None else self.readback,
            )
        return response

    def head_object(self, **kwargs):
        self.calls.append(('head_object', kwargs.copy()))
        return self._read_response()

    def get_object(self, **kwargs):
        self.calls.append(('get_object', kwargs.copy()))
        response = self._read_response(body=True)
        if self.after_get is not None:
            self.after_get()
        return response


def _claimed_private_run(slug, payload, *, listing_count=1):
    tenant = Tenant.objects.create(name=f'Artifact {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Artifact {slug}',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-artifact-test-credentials',
        feed_intent_revision=1,
        feed_intent_dispatched_revision=1,
    )
    owner_digest = account_identity_digest(account)
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='artifact-hmac-v1',
        owner_identity_digest=owner_digest,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        source_intent_revision=1,
    )
    due_at = timezone.now() - timedelta(seconds=1)
    run = MarketplaceFeedRun.objects.create(
        tenant=tenant,
        account=account,
        marketplace=account.marketplace,
        account_identity_digest=owner_digest,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        state=MarketplaceFeedRun.State.PREPARING,
        next_attempt_at=due_at,
        total_count=listing_count,
        pending_count=listing_count,
        source_intent_revision=1,
        endpoint_revision=0,
        predecessor_artifact_id=None,
        artifact_upload_attempt=0,
    )
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        now=timezone.now(),
    )
    assert claim is not None
    return account, endpoint, run, claim


def _service(client, *, max_bytes=1024 * 1024):
    return PrivateFeedArtifactStorageService(
        client=client,
        bucket='private-feed-artifacts',
        expected_bucket_owner='private-artifact-owner-1',
        max_bytes=max_bytes,
    )


def _upload(service, claim, payload, *, listing_count=1):
    with tempfile.TemporaryFile(mode='w+b') as payload_file:
        payload_file.write(payload)
        payload_file.flush()
        return service.upload_and_attach(
            claim,
            payload_file=payload_file,
            projection_count=listing_count,
        )


def test_private_exact_version_is_read_back_and_attached_without_promotion():
    payload = b'<Ads formatVersion="3" target="Avito.ru"></Ads>'
    account, endpoint, run, claim = _claimed_private_run(
        'artifact-storage-success',
        payload,
    )
    client = FakePrivateS3Client()

    artifact = _upload(_service(client), claim, payload)

    run.refresh_from_db()
    endpoint.refresh_from_db()
    assert artifact == run.feed_artifact
    assert artifact.account_id == account.pk
    assert artifact.endpoint_id == endpoint.pk
    assert artifact.object_version_id == 'artifact-version-1'
    assert artifact.payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert artifact.size_bytes == len(payload)
    assert endpoint.current_artifact_id is None
    assert endpoint.artifact_revision == 0
    assert endpoint.serve_enabled is False
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
    assert attempt.projection_count == 1
    assert attempt.object_version_id == artifact.object_version_id
    assert attempt.put_resolution_source == 'put_response'
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()

    put_name, put_kwargs = client.calls[0]
    assert put_name == 'put_object_once'
    assert 'ACL' not in put_kwargs
    assert put_kwargs['Bucket'] == 'private-feed-artifacts'
    assert put_kwargs['ExpectedBucketOwner'] == 'private-artifact-owner-1'
    assert put_kwargs['ContentType'] == MarketplaceFeedArtifact.CONTENT_TYPE_XML
    assert put_kwargs['ContentLength'] == len(payload)
    assert put_kwargs['ChecksumSHA256'] == base64.b64encode(
        hashlib.sha256(payload).digest(),
    ).decode('ascii')
    assert account.name not in put_kwargs['Key']
    assert put_kwargs['Key'] == (
        f'private-feeds/v1/{endpoint.pk}/{run.pk}/00001/feed.xml'
    )
    assert put_kwargs['Metadata']['owner-identity-digest'] == claim.account_identity_digest
    assert put_kwargs['Metadata']['run-revision'] == str(claim.revision)
    assert put_kwargs['Metadata']['source-intent-revision'] == '1'
    assert put_kwargs['Metadata']['endpoint-revision'] == '0'

    for operation, kwargs in client.calls[1:]:
        assert operation in {'head_object', 'get_object'}
        assert kwargs == {
            'Bucket': 'private-feed-artifacts',
            'Key': put_kwargs['Key'],
            'VersionId': 'artifact-version-1',
            'ChecksumMode': 'ENABLED',
            'ExpectedBucketOwner': 'private-artifact-owner-1',
        }


def test_prepared_and_put_pending_are_separate_durable_boundaries():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-durable-boundaries',
        payload,
    )
    observed_states = []

    class BoundaryObservingClient(FakePrivateS3Client):
        def put_object_once(self, **kwargs):
            observed_states.append(
                MarketplaceFeedArtifactUploadAttempt.objects.get(run=run).state,
            )
            return super().put_object_once(**kwargs)

    original_begin = storage_module._begin_put_attempt

    def observe_prepared(*args, **kwargs):
        observed_states.append(
            MarketplaceFeedArtifactUploadAttempt.objects.get(
                pk=kwargs['attempt_id'],
            ).state,
        )
        return original_begin(*args, **kwargs)

    client = BoundaryObservingClient()
    with patch(
        'apps.marketplaces.feed_artifact_storage._begin_put_attempt',
        side_effect=observe_prepared,
    ):
        artifact = _upload(_service(client), claim, payload)

    assert artifact.run_id == run.pk
    assert observed_states == [
        MarketplaceFeedArtifactUploadAttempt.State.PREPARED,
        MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
    ]


@pytest.mark.django_db(transaction=True)
def test_storage_boundary_rejects_a_caller_outer_transaction():
    """No object PUT may follow a ledger row that is only a savepoint."""

    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-nested-transaction',
        payload,
    )
    client = FakePrivateS3Client()

    with transaction.atomic(), pytest.raises(
        RuntimeError,
        match='durable atomic block cannot be nested',
    ):
        _upload(_service(client), claim, payload)

    assert client.calls == []
    assert not MarketplaceFeedArtifactUploadAttempt.objects.filter(
        run=run,
    ).exists()


def test_stale_post_upload_revision_leaves_verified_exact_version_orphaned():
    payload = b'<Ads></Ads>'
    account, endpoint, run, claim = _claimed_private_run(
        'artifact-storage-stale',
        payload,
    )

    def advance_source_revision():
        with transaction.atomic():
            MarketplaceAccount.all_objects.select_for_update().filter(
                pk=account.pk,
            ).update(feed_intent_revision=2)
            MarketplaceFeedEndpoint.objects.select_for_update().filter(
                pk=endpoint.pk,
            ).update(source_intent_revision=2)

    client = FakePrivateS3Client(after_get=advance_source_revision)

    with pytest.raises(OrphanedFeedArtifactUpload) as exc_info:
        _upload(_service(client), claim, payload)

    error = exc_info.value
    assert error.stale is True
    assert error.locator.object_version_id == 'artifact-version-1'
    assert error.locator.object_key.endswith(f'/{run.pk}/00001/feed.xml')
    assert not MarketplaceFeedArtifact.objects.filter(run=run).exists()
    run.refresh_from_db()
    assert run.feed_artifact_id is None
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ORPHANED

    # Even if the mutable source happens to return to its old revision, an
    # ORPHANED exact version is terminal and must never authorize a new PUT.
    with transaction.atomic():
        MarketplaceAccount.all_objects.select_for_update().filter(
            pk=account.pk,
        ).update(feed_intent_revision=1)
        MarketplaceFeedEndpoint.objects.select_for_update().filter(
            pk=endpoint.pk,
        ).update(source_intent_revision=1)
    with pytest.raises(FeedArtifactAttemptBlocked):
        _upload(_service(client), claim, payload)
    assert [name for name, _ in client.calls].count('put_object_once') == 1


def test_missing_put_version_fails_closed_without_head_or_attachment():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-no-version',
        payload,
    )
    client = FakePrivateS3Client(version_id=None)

    with pytest.raises(FeedArtifactUploadOutcomeUnknown) as exc_info:
        _upload(_service(client), claim, payload)

    assert exc_info.value.locator.object_version_id is None
    assert [name for name, _ in client.calls] == ['put_object_once']
    assert not MarketplaceFeedArtifact.objects.filter(run=run).exists()
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING

    with pytest.raises(FeedArtifactUploadOutcomeUnknown):
        _upload(_service(client), claim, payload)
    assert [name for name, _ in client.calls] == ['put_object_once']


@pytest.mark.parametrize('missing_checksum_phase', ('put', 'head'))
def test_missing_provider_checksum_uses_exact_version_readback(
    missing_checksum_phase,
):
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        f'artifact-storage-no-checksum-{missing_checksum_phase}',
        payload,
    )
    client = FakePrivateS3Client(
        put_checksum=missing_checksum_phase != 'put',
        head_checksum=missing_checksum_phase != 'head',
    )

    artifact = _upload(_service(client), claim, payload)

    assert artifact.run_id == run.pk
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
    assert [name for name, _ in client.calls] == [
        'put_object_once',
        'head_object',
        'get_object',
    ]


def test_yandex_title_case_metadata_without_checksum_uses_full_readback():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-yandex-metadata',
        payload,
    )

    class YandexResponseClient(FakePrivateS3Client):
        def _read_response(self, *, body=False):
            response = super()._read_response(body=body)
            response.pop('ChecksumSHA256', None)
            response['Metadata'] = {
                '-'.join(part.capitalize() for part in key.split('-')): value
                for key, value in response['Metadata'].items()
            }
            return response

    client = YandexResponseClient(put_checksum=False, head_checksum=False)

    artifact = _upload(_service(client), claim, payload)

    assert artifact.run_id == run.pk
    assert artifact.payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert [name for name, _ in client.calls].count('put_object_once') == 1


def test_present_mismatched_readback_checksum_fails_closed():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-head-checksum-mismatch',
        payload,
    )

    class MismatchedReadbackChecksumClient(FakePrivateS3Client):
        def _read_response(self, *, body=False):
            response = super()._read_response(body=body)
            response['ChecksumSHA256'] = base64.b64encode(
                b'wrong checksum',
            ).decode('ascii')
            return response

    client = MismatchedReadbackChecksumClient()

    with pytest.raises(FeedArtifactVerificationError) as exc_info:
        _upload(_service(client), claim, payload)

    assert exc_info.value.locator.object_version_id == 'artifact-version-1'
    assert not MarketplaceFeedArtifact.objects.filter(run=run).exists()
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN


def test_case_colliding_response_metadata_fails_closed():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-metadata-collision',
        payload,
    )

    class CollidingMetadataClient(FakePrivateS3Client):
        def _read_response(self, *, body=False):
            response = super()._read_response(body=body)
            response['Metadata']['Payload-Sha256'] = (
                response['Metadata']['payload-sha256']
            )
            return response

    with pytest.raises(FeedArtifactVerificationError):
        _upload(_service(CollidingMetadataClient()), claim, payload)

    assert not MarketplaceFeedArtifact.objects.filter(run=run).exists()
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN


def test_mismatched_put_checksum_keeps_exact_version_unattached():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-put-checksum-mismatch',
        payload,
    )

    class MismatchedPutChecksumClient(FakePrivateS3Client):
        def put_object_once(self, **kwargs):
            response = super().put_object_once(**kwargs)
            response['ChecksumSHA256'] = base64.b64encode(b'wrong checksum').decode('ascii')
            return response

    client = MismatchedPutChecksumClient()

    with pytest.raises(FeedArtifactUploadOutcomeUnknown) as exc_info:
        _upload(_service(client), claim, payload)

    assert exc_info.value.locator.object_version_id == 'artifact-version-1'
    assert [name for name, _ in client.calls] == ['put_object_once']
    assert not MarketplaceFeedArtifact.objects.filter(run=run).exists()
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
    assert attempt.object_version_id == 'artifact-version-1'
    assert attempt.put_resolution_source == 'put_response'
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()

    artifact = _upload(_service(client), claim, payload)
    assert artifact.run_id == run.pk
    assert [name for name, _ in client.calls].count('put_object_once') == 1
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


def test_returned_version_is_journaled_even_when_lease_expires_during_put():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-version-after-lease',
        payload,
    )

    def expire_lease():
        MarketplaceFeedRun.objects.filter(pk=run.pk).update(
            claimed_until=timezone.now() - timedelta(seconds=1),
            updated_at=timezone.now(),
        )

    client = FakePrivateS3Client(after_put=expire_lease)
    with pytest.raises(FeedArtifactResumeRequired) as exc_info:
        _upload(_service(client), claim, payload)

    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
    assert attempt.object_version_id == 'artifact-version-1'
    assert attempt.put_resolution_source == 'put_response'
    assert exc_info.value.locator.object_version_id == attempt.object_version_id
    assert [name for name, _ in client.calls] == ['put_object_once']

    run.refresh_from_db()
    renewed_claim = claim_due_run_for_account(
        run.account_id,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        now=timezone.now(),
    )
    assert renewed_claim is not None
    assert renewed_claim.revision > claim.revision

    artifact = _upload(_service(client), renewed_claim, payload)
    attempt.refresh_from_db()
    assert artifact.run_id == run.pk
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
    assert [name for name, _ in client.calls].count('put_object_once') == 1


@pytest.mark.parametrize('peer_progress', ('verified', 'attached'))
def test_peer_verification_or_attachment_progress_is_idempotent(peer_progress):
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        f'artifact-storage-peer-{peer_progress}',
        payload,
    )
    local = storage_module._LocalPayload(
        size_bytes=len(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        projection_count=1,
    )

    def advance_peer():
        attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
        decision = storage_module._record_verified_or_observe_progress(
            claim,
            attempt_id=attempt.pk,
            local=local,
            bucket='private-feed-artifacts',
            expected_bucket_owner='private-artifact-owner-1',
        )
        assert decision.action == storage_module._ACTION_ATTACH
        if peer_progress == 'attached':
            decision = storage_module._attach_verified_attempt(
                claim,
                attempt_id=attempt.pk,
                local=local,
                bucket='private-feed-artifacts',
                expected_bucket_owner='private-artifact-owner-1',
            )
            assert decision.action == storage_module._ACTION_ATTACHED

    client = FakePrivateS3Client(after_get=advance_peer)
    artifact = _upload(_service(client), claim, payload)

    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
    assert artifact == run.__class__.objects.get(pk=run.pk).feed_artifact
    assert [name for name, _ in client.calls].count('put_object_once') == 1


def test_readback_digest_mismatch_keeps_exact_version_unattached():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-readback-mismatch',
        payload,
    )
    client = FakePrivateS3Client(readback=b'<Bad></Bad>')

    with pytest.raises(FeedArtifactVerificationError) as exc_info:
        _upload(_service(client), claim, payload)

    assert exc_info.value.locator.object_version_id == 'artifact-version-1'
    assert not MarketplaceFeedArtifact.objects.filter(run=run).exists()


def test_non_disk_or_over_cap_payload_never_reaches_private_storage():
    payload = b'<Ads></Ads>'
    _, _, _, claim = _claimed_private_run(
        'artifact-storage-disk-cap',
        payload,
    )
    client = FakePrivateS3Client()

    with pytest.raises(FeedArtifactContentError):
        _service(client).upload_and_attach(
            claim,
            payload_file=io.BytesIO(payload),
            projection_count=1,
        )
    with pytest.raises(FeedArtifactContentError):
        _upload(_service(client, max_bytes=len(payload) - 1), claim, payload)

    assert client.calls == []


def test_payload_checksum_and_stale_claim_are_rejected_before_put():
    payload = b'<Ads></Ads>'
    account, _, _, claim = _claimed_private_run(
        'artifact-storage-pre-put-fence',
        payload,
    )
    client = FakePrivateS3Client()

    with pytest.raises(FeedArtifactContentError):
        _upload(_service(client), claim, b'<Different></Different>')
    MarketplaceAccount.all_objects.filter(pk=account.pk).update(is_active=False)
    with pytest.raises(StaleFeedArtifactClaim):
        _upload(_service(client), claim, payload)

    assert client.calls == []


@pytest.mark.parametrize(
    'overrides',
    (
        {'client': None},
        {'bucket': ''},
        {'bucket': 'Public_Bucket'},
        {'expected_bucket_owner': ''},
        {'max_bytes': None},
        {'max_bytes': 0},
        {'max_bytes': 1_073_741_825},
    ),
)
def test_private_storage_configuration_never_falls_back(overrides):
    values = {
        'client': FakePrivateS3Client(),
        'bucket': 'private-feed-artifacts',
        'expected_bucket_owner': 'private-artifact-owner-1',
        'max_bytes': 1024,
    }
    values.update(overrides)

    with pytest.raises(FeedArtifactConfigurationError):
        PrivateFeedArtifactStorageService(**values)


@pytest.mark.parametrize('attestation', (True, 0, 2, None))
def test_retrying_or_invalid_write_attestation_is_rejected(attestation):
    client = FakePrivateS3Client()
    client.put_total_max_attempts = attestation

    with pytest.raises(FeedArtifactConfigurationError):
        _service(client)


def test_ordinary_retrying_sdk_shape_is_rejected():
    class OrdinarySdkClient:
        def put_object(self, **kwargs):
            raise AssertionError('must never be called')

        def head_object(self, **kwargs):
            raise AssertionError('must never be called')

        def get_object(self, **kwargs):
            raise AssertionError('must never be called')

    with pytest.raises(FeedArtifactConfigurationError):
        _service(OrdinarySdkClient())


def test_write_attestation_is_rechecked_immediately_before_put():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-mutated-attestation',
        payload,
    )
    client = FakePrivateS3Client()
    service = _service(client)
    original_begin = storage_module._begin_put_attempt

    def mutate_after_boundary(*args, **kwargs):
        result = original_begin(*args, **kwargs)
        client.put_total_max_attempts = 2
        return result

    with (
        patch(
            'apps.marketplaces.feed_artifact_storage._begin_put_attempt',
            side_effect=mutate_after_boundary,
        ),
        pytest.raises(FeedArtifactConfigurationError),
    ):
        _upload(service, claim, payload)

    assert client.calls == []
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING


def test_only_confirmed_no_object_allows_next_exact_attempt():
    payload = b'<Ads></Ads>'
    _, _, run, claim = _claimed_private_run(
        'artifact-storage-no-object-retry',
        payload,
    )
    unknown_client = FakePrivateS3Client(version_id=None)
    with pytest.raises(FeedArtifactUploadOutcomeUnknown):
        _upload(_service(unknown_client), claim, payload)

    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(run=run)
    resolved_at = timezone.now()
    put_started_at = resolved_at - timedelta(minutes=21)
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
    with transaction.atomic():
        MarketplaceFeedPutReconciliationAudit.objects.create(
            attempt=attempt,
            pre_revision=attempt.revision,
            post_revision=attempt.revision + 1,
            from_state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            to_state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
            outcome=(
                MarketplaceFeedPutReconciliationAudit.Outcome.
                NO_OBJECT_BY_REVIEWED_SETTLEMENT_POLICY
            ),
            decision_code='reviewed_settlement_no_object',
            version_id_captured=False,
            origin_process_identity_digest='c' * 64,
            operator_identity_digest='d' * 64,
            evidence_digest='e' * 64,
            digest_scheme_revision='hmac-sha256-v1',
            identity_digest_key_revision='identity-key-2026-08',
            adapter_policy_revision='list-versions-v1',
            canary_policy_revision='canary-2026-08-20',
            origin_process_terminated_at=(
                resolved_at - timedelta(minutes=20)
            ),
            reconciliation_started_at=resolved_at - timedelta(minutes=2),
            decision_at=resolved_at,
            settlement_window_seconds=15 * 60,
            pages_scanned=1,
            entries_scanned=0,
            exact_version_count=0,
            exact_delete_marker_count=0,
        )
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
            revision=F('revision') + 1,
            put_resolution_source=(
                MarketplaceFeedArtifactUploadAttempt.ResolutionSource.
                OPERATOR_RECONCILIATION
            ),
            resolved_at=resolved_at,
            safe_error_code='reviewed_settlement_no_object',
            updated_at=resolved_at,
        )
    assert changed == 1

    retry_client = FakePrivateS3Client(version_id='artifact-version-2')
    artifact = _upload(_service(retry_client), claim, payload)

    attempts = list(
        MarketplaceFeedArtifactUploadAttempt.objects.filter(run=run).order_by(
            'attempt_no',
        ),
    )
    assert [item.state for item in attempts] == [
        MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
        MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
    ]
    assert attempts[0].put_resolution_source == 'operator_reconciliation'
    assert attempts[1].put_resolution_source == 'put_response'
    assert MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempts[0],
    ).exists()
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempts[1],
    ).exists()
    assert artifact.upload_attempt == 2
    assert unknown_client.calls[0][1]['Key'].endswith('/00001/feed.xml')
    assert retry_client.calls[0][1]['Key'].endswith('/00002/feed.xml')


@override_settings(MARKETPLACE_FEED_ARTIFACT_MODE='disabled')
def test_disabled_artifact_mode_rejects_explicit_service_before_put():
    payload = b'<Ads></Ads>'
    _, _, _, claim = _claimed_private_run(
        'artifact-storage-disabled',
        payload,
    )
    client = FakePrivateS3Client()

    with pytest.raises(FeedArtifactConfigurationError):
        _upload(_service(client), claim, payload)

    assert client.calls == []
