import os
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.db.models import F
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.marketplaces import feed_artifact_put_reconciliation as reconciliation_module
from apps.marketplaces.feed_artifact_put_reconciliation import (
    LIST_PAGE_SIZE,
    MANUAL_DELETE_MARKER,
    MANUAL_MALFORMED_LISTING,
    MANUAL_MULTIPLE_VERSIONS,
    MANUAL_PAGE_LIMIT,
    MANUAL_UNUSABLE_VERSION,
    NO_OBJECT_AUDIT_CODE,
    OUTCOME_NO_OBJECT,
    OUTCOME_SUPERSEDED,
    OUTCOME_VERSION_KNOWN,
    PutOriginTerminationAttestation,
    PutPendingAttemptReference,
    PutPendingReconciliationError,
    PUT_PENDING_SETTLEMENT_WINDOW_SECONDS,
    reconcile_put_pending_upload_attempt,
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


OWNER_DIGEST = 'a' * 64
PAYLOAD_DIGEST = 'b' * 64
BUCKET = 'private-feed-artifacts'
BUCKET_OWNER = 'cloud:owner/account-123'
EVIDENCE_DIGEST = 'c' * 64
OPERATOR_DIGEST = 'd' * 64
ORIGIN_PROCESS_DIGEST = 'e' * 64
DEFAULT_PUT_AGE = timedelta(hours=1)


class FakeAuthoritativeVersionClient:
    authoritative_exact_key_version_listing = True
    adapter_policy_revision = 'list-versions-v1'
    canary_policy_revision = 'canary-2026-08-20'

    def __init__(self, pages=(), *, failure=None, before_first_list=None):
        self.pages = list(pages)
        self.failure = failure
        self.before_first_list = before_first_list
        self.calls = []
        self.atomic_states = []

    def list_object_versions(self, **kwargs):
        self.atomic_states.append(connection.in_atomic_block)
        self.calls.append(kwargs)
        if self.before_first_list is not None and len(self.calls) == 1:
            self.before_first_list()
        if self.failure is not None and len(self.calls) > len(self.pages):
            raise self.failure
        return self.pages[len(self.calls) - 1]


def _pending_context(*, put_age=DEFAULT_PUT_AGE):
    suffix = uuid.uuid4().hex[:16]
    tenant = Tenant.objects.create(
        name=f'PUT reconciliation {suffix}',
        slug=f'put-reconcile-{suffix}',
    )
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'PUT reconciliation {suffix}',
        external_id=f'put-reconcile-{suffix}',
        credentials_enc=b'opaque-test-credentials',
        feed_intent_revision=1,
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=OWNER_DIGEST,
        source_intent_revision=1,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
    )
    run = MarketplaceFeedRun.objects.create(
        tenant=tenant,
        account=account,
        marketplace=account.marketplace,
        account_identity_digest=OWNER_DIGEST,
        payload_sha256=PAYLOAD_DIGEST,
        source_intent_revision=1,
        endpoint_revision=0,
        claim_token=uuid.uuid4(),
        claimed_until=timezone.now() + timedelta(hours=1),
    )
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.create(
        account=account,
        endpoint=endpoint,
        run=run,
        attempt_no=1,
        storage_bucket=BUCKET,
        expected_bucket_owner=BUCKET_OWNER,
        object_key=(
            f'private-feeds/v1/{endpoint.pk}/{run.pk}/00001/feed.xml'
        ),
        payload_sha256=PAYLOAD_DIGEST,
        size_bytes=1024,
        projection_count=3,
        content_type=MarketplaceFeedArtifact.CONTENT_TYPE_XML,
    )
    put_started_at = timezone.now() - put_age
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = 'replica'")
        cursor.execute(
            '''
            UPDATE marketplaces_marketplacefeedartifactuploadattempt
               SET created_at = %s,
                   updated_at = %s,
                   state = %s,
                   revision = 1,
                   put_run_revision = %s,
                   put_started_at = %s
             WHERE id = %s
            ''',
            [
                put_started_at - timedelta(seconds=1),
                put_started_at,
                MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
                run.revision,
                put_started_at,
                attempt.pk,
            ],
        )
        assert cursor.rowcount == 1
    attempt.refresh_from_db()
    reference = PutPendingAttemptReference(
        tenant_id=tenant.pk,
        account_id=account.pk,
        endpoint_id=endpoint.pk,
        run_id=run.pk,
        attempt_id=attempt.pk,
        expected_revision=attempt.revision,
    )
    termination = PutOriginTerminationAttestation(
        evidence_reference=f'incident-{suffix}',
        evidence_digest=EVIDENCE_DIGEST,
        operator_identity_digest=OPERATOR_DIGEST,
        origin_process_identity_digest=ORIGIN_PROCESS_DIGEST,
        digest_scheme_revision='hmac-sha256-v1',
        identity_digest_key_revision='identity-key-2026-08',
        origin_process_id=os.getpid() + 10_000,
        origin_process_terminated_at=(
            put_started_at + timedelta(minutes=1)
        ),
        operator_confirmed=True,
    )
    return attempt, reference, termination


def _page(
    attempt,
    *,
    versions=(),
    delete_markers=(),
    truncated=False,
    next_markers=None,
):
    response = {
        'Name': attempt.storage_bucket,
        'Prefix': attempt.object_key,
        'MaxKeys': LIST_PAGE_SIZE,
        'Versions': list(versions),
        'DeleteMarkers': list(delete_markers),
        'IsTruncated': truncated,
    }
    if next_markers is not None:
        response['NextKeyMarker'], response['NextVersionIdMarker'] = (
            next_markers
        )
    return response


def _version(attempt, version_id, *, key=None, size=None, is_latest=True):
    return {
        'Key': key or attempt.object_key,
        'VersionId': version_id,
        'Size': attempt.size_bytes if size is None else size,
        'IsLatest': is_latest,
    }


@pytest.mark.django_db(transaction=True)
def test_reviewed_settled_absence_transitions_to_no_object_without_io_locks():
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])

    public_attestation_repr = repr(termination)
    validated_attestation_repr = repr(
        reconciliation_module._validate_termination_attestation(termination),
    )
    for sensitive_value in (
        termination.evidence_reference,
        termination.evidence_digest,
        termination.operator_identity_digest,
        termination.origin_process_identity_digest,
    ):
        assert sensitive_value not in public_attestation_repr
        assert sensitive_value not in validated_attestation_repr

    result = reconcile_put_pending_upload_attempt(
        reference,
        client=client,
        termination=termination,
    )

    attempt.refresh_from_db()
    assert result.outcome == OUTCOME_NO_OBJECT
    assert result.applied is True
    assert result.pages_scanned == 1
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
    assert attempt.safe_error_code == NO_OBJECT_AUDIT_CODE
    assert attempt.resolved_at is not None
    assert attempt.put_resolution_source == 'operator_reconciliation'
    audit = MarketplaceFeedPutReconciliationAudit.objects.get(attempt=attempt)
    assert audit.attempt.account_id == reference.account_id
    assert audit.attempt.endpoint_id == reference.endpoint_id
    assert audit.attempt.run_id == reference.run_id
    assert audit.attempt.run.tenant_id == reference.tenant_id
    assert audit.pre_revision == reference.expected_revision
    assert audit.post_revision == result.revision
    assert audit.from_state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    assert audit.to_state == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
    assert audit.outcome == OUTCOME_NO_OBJECT
    assert audit.decision_code == NO_OBJECT_AUDIT_CODE
    assert audit.version_id_captured is False
    assert audit.evidence_digest == EVIDENCE_DIGEST
    assert audit.operator_identity_digest == OPERATOR_DIGEST
    assert audit.origin_process_identity_digest == ORIGIN_PROCESS_DIGEST
    assert audit.digest_scheme_revision == 'hmac-sha256-v1'
    assert audit.identity_digest_key_revision == 'identity-key-2026-08'
    assert audit.adapter_policy_revision == client.adapter_policy_revision
    assert audit.canary_policy_revision == client.canary_policy_revision
    assert (
        audit.settlement_window_seconds
        == PUT_PENDING_SETTLEMENT_WINDOW_SECONDS
    )
    assert audit.pages_scanned == 1
    assert audit.entries_scanned == 0
    assert audit.exact_version_count == 0
    assert audit.exact_delete_marker_count == 0
    assert client.atomic_states == [False]
    assert client.calls == [{
        'Bucket': BUCKET,
        'Prefix': attempt.object_key,
        'ExpectedBucketOwner': BUCKET_OWNER,
        'MaxKeys': LIST_PAGE_SIZE,
    }]
    rendered = repr(result)
    assert BUCKET not in rendered
    assert attempt.object_key not in rendered


@pytest.mark.django_db(transaction=True)
def test_one_usable_exact_version_is_captured_but_never_returned():
    attempt, reference, termination = _pending_context()
    secret_version_id = 'opaque-exact-version-id'
    client = FakeAuthoritativeVersionClient([
        _page(
            attempt,
            versions=[_version(attempt, secret_version_id)],
        ),
    ])

    result = reconcile_put_pending_upload_attempt(
        reference,
        client=client,
        termination=termination,
    )

    attempt.refresh_from_db()
    assert result.outcome == OUTCOME_VERSION_KNOWN
    assert result.exact_version_count == 1
    assert result.applied is True
    assert (
        attempt.state
        == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
    )
    assert attempt.object_version_id == secret_version_id
    assert attempt.version_known_at is not None
    assert attempt.put_resolution_source == 'operator_reconciliation'
    audit = MarketplaceFeedPutReconciliationAudit.objects.get(attempt=attempt)
    assert audit.version_id_captured is True
    assert audit.outcome == OUTCOME_VERSION_KNOWN
    assert audit.decision_code == ''
    assert secret_version_id not in repr(result)
    assert secret_version_id not in repr(audit)


@pytest.mark.django_db(transaction=True)
def test_multiple_versions_and_delete_marker_never_guess_or_delete():
    first, reference, termination = _pending_context()
    multiple_client = FakeAuthoritativeVersionClient([
        _page(
            first,
            versions=[
                _version(first, 'version-2', is_latest=True),
                _version(first, 'version-1', is_latest=False),
            ],
        ),
    ])
    multiple_result = reconcile_put_pending_upload_attempt(
        reference,
        client=multiple_client,
        termination=termination,
    )
    first.refresh_from_db()

    second, second_reference, second_termination = _pending_context()
    marker_version_id = 'version-addressable-behind-delete-marker'
    marker_client = FakeAuthoritativeVersionClient([
        _page(
            second,
            versions=[_version(second, marker_version_id, is_latest=False)],
            delete_markers=[{
                'Key': second.object_key,
                'VersionId': 'delete-marker-version',
                'IsLatest': True,
            }],
        ),
    ])
    marker_result = reconcile_put_pending_upload_attempt(
        second_reference,
        client=marker_client,
        termination=second_termination,
    )
    second.refresh_from_db()

    assert multiple_result.outcome == 'manual_review'
    assert first.safe_error_code == MANUAL_MULTIPLE_VERSIONS
    assert marker_result.outcome == 'manual_review'
    assert second.safe_error_code == MANUAL_DELETE_MARKER
    assert first.object_version_id is None
    assert second.object_version_id == marker_version_id
    assert second.version_known_at is not None
    assert first.put_resolution_source == 'operator_reconciliation'
    assert second.put_resolution_source == 'operator_reconciliation'
    first_audit = MarketplaceFeedPutReconciliationAudit.objects.get(
        attempt=first,
    )
    second_audit = MarketplaceFeedPutReconciliationAudit.objects.get(
        attempt=second,
    )
    assert first_audit.outcome == 'manual_review'
    assert first_audit.decision_code == MANUAL_MULTIPLE_VERSIONS
    assert first_audit.version_id_captured is False
    assert second_audit.outcome == 'manual_review'
    assert second_audit.decision_code == MANUAL_DELETE_MARKER
    assert second_audit.version_id_captured is True
    assert marker_version_id not in repr(marker_result)


@pytest.mark.django_db(transaction=True)
def test_one_known_version_id_survives_manual_metadata_review():
    attempt, reference, termination = _pending_context()
    version_id = 'version-with-size-anomaly'
    client = FakeAuthoritativeVersionClient([
        _page(
            attempt,
            versions=[_version(
                attempt,
                version_id,
                size=attempt.size_bytes + 1,
            )],
        ),
    ])

    result = reconcile_put_pending_upload_attempt(
        reference,
        client=client,
        termination=termination,
    )

    attempt.refresh_from_db()
    assert result.outcome == 'manual_review'
    assert attempt.safe_error_code == MANUAL_UNUSABLE_VERSION
    assert attempt.object_version_id == version_id
    assert attempt.version_known_at is not None
    assert version_id not in repr(result)


@pytest.mark.django_db(transaction=True)
def test_unique_known_version_survives_duplicate_and_malformed_entries():
    attempt, reference, termination = _pending_context()
    version_id = 'one-unique-addressable-version'
    client = FakeAuthoritativeVersionClient([
        _page(
            attempt,
            versions=[
                _version(attempt, version_id),
                _version(attempt, version_id, is_latest=False),
                _version(attempt, None, is_latest=False),
            ],
        ),
    ])

    result = reconcile_put_pending_upload_attempt(
        reference,
        client=client,
        termination=termination,
    )

    attempt.refresh_from_db()
    assert result.outcome == 'manual_review'
    assert result.exact_version_count == 3
    assert attempt.safe_error_code == MANUAL_MULTIPLE_VERSIONS
    assert attempt.object_version_id == version_id
    assert version_id not in repr(result)


@pytest.mark.django_db(transaction=True)
def test_malformed_or_unbounded_listing_fails_closed_to_manual_review():
    malformed, reference, termination = _pending_context()
    malformed_version_id = 'known-before-malformed-page'
    malformed_client = FakeAuthoritativeVersionClient([
        _page(
            malformed,
            versions=[_version(malformed, malformed_version_id)],
            truncated=True,
            next_markers=(
                f'{malformed.object_key}.marker',
                'malformed-next-version-marker',
            ),
        ),
        {
            'Versions': [],
            'DeleteMarkers': [],
            'IsTruncated': 'false',
        },
    ])
    malformed_result = reconcile_put_pending_upload_attempt(
        reference,
        client=malformed_client,
        termination=termination,
    )
    malformed.refresh_from_db()

    paged, paged_reference, paged_termination = _pending_context()
    paged_version_id = 'known-before-page-limit'
    pages = []
    for page_number in range(1, 5):
        versions = [_version(
            paged,
            f'sibling-version-{page_number}',
            key=f'{paged.object_key}.sibling-{page_number}',
        )]
        if page_number == 1:
            versions.append(_version(paged, paged_version_id))
        pages.append(_page(
            paged,
            versions=versions,
            truncated=True,
            next_markers=(
                f'{paged.object_key}.marker-{page_number}',
                f'marker-version-{page_number}',
            ),
        ))
    page_client = FakeAuthoritativeVersionClient(pages)
    page_result = reconcile_put_pending_upload_attempt(
        paged_reference,
        client=page_client,
        termination=paged_termination,
    )
    paged.refresh_from_db()

    assert malformed_result.outcome == 'manual_review'
    assert malformed.safe_error_code == MANUAL_MALFORMED_LISTING
    assert malformed.object_version_id == malformed_version_id
    assert malformed_version_id not in repr(malformed_result)
    assert page_result.outcome == 'manual_review'
    assert page_result.pages_scanned == 4
    assert len(page_client.calls) == 4
    assert paged.safe_error_code == MANUAL_PAGE_LIMIT
    assert paged.object_version_id == paged_version_id
    assert paged_version_id not in repr(page_result)


@pytest.mark.django_db(transaction=True)
def test_missing_scope_empty_truncation_and_outside_marker_are_malformed():
    class ExplodingMapping(dict):
        def get(self, key, default=None):
            raise RuntimeError('secret malformed mapping detail')

    missing, missing_reference, missing_termination = _pending_context()
    empty, empty_reference, empty_termination = _pending_context()
    outside, outside_reference, outside_termination = _pending_context()

    cases = [
        (
            missing,
            missing_reference,
            missing_termination,
            {
                'Versions': [],
                'DeleteMarkers': [],
                'IsTruncated': False,
            },
        ),
        (
            empty,
            empty_reference,
            empty_termination,
            _page(
                empty,
                truncated=True,
                next_markers=(empty.object_key, 'empty-version-marker'),
            ),
        ),
        (
            outside,
            outside_reference,
            outside_termination,
            _page(
                outside,
                versions=[_version(
                    outside,
                    'sibling-version',
                    key=f'{outside.object_key}.sibling',
                )],
                truncated=True,
                next_markers=('outside-prefix', 'outside-version-marker'),
            ),
        ),
    ]

    for attempt, reference, termination, response in cases:
        client = FakeAuthoritativeVersionClient([response])
        result = reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=termination,
        )
        attempt.refresh_from_db()
        assert result.outcome == 'manual_review'
        assert attempt.safe_error_code == MANUAL_MALFORMED_LISTING
        assert attempt.object_version_id is None
        assert len(client.calls) == 1

    exploding, exploding_reference, exploding_termination = _pending_context()
    exploding_client = FakeAuthoritativeVersionClient([ExplodingMapping()])
    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            exploding_reference,
            client=exploding_client,
            termination=exploding_termination,
        )
    exploding.refresh_from_db()
    assert exc_info.value.code == 'version_listing_parse_failed'
    assert 'secret malformed mapping detail' not in str(exc_info.value)
    assert (
        exploding.state
        == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    )
    assert exploding.revision == exploding_reference.expected_revision
    assert exploding.safe_error_code == ''
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=exploding,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_transport_failure_after_a_page_makes_zero_database_mutation():
    attempt, reference, termination = _pending_context()
    first_page = _page(
        attempt,
        versions=[_version(
            attempt,
            'sibling-version',
            key=f'{attempt.object_key}.sibling',
        )],
        truncated=True,
        next_markers=(f'{attempt.object_key}.marker', 'marker-version'),
    )
    client = FakeAuthoritativeVersionClient(
        [first_page],
        failure=RuntimeError('secret transport details'),
    )

    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=termination,
        )

    attempt.refresh_from_db()
    assert exc_info.value.code == 'version_listing_transport_failed'
    assert 'secret transport details' not in str(exc_info.value)
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    assert attempt.revision == reference.expected_revision
    assert attempt.safe_error_code == ''
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_settlement_and_current_process_contract_prevent_early_listing():
    attempt, reference, termination = _pending_context(
        put_age=timedelta(minutes=10),
    )
    early_client = FakeAuthoritativeVersionClient([_page(attempt)])

    early = reconcile_put_pending_upload_attempt(
        reference,
        client=early_client,
        termination=termination,
    )

    assert early.outcome == 'settlement_pending'
    assert early.settlement_remaining_seconds > 0
    assert early_client.calls == []
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()

    current_process = replace(
        termination,
        origin_process_id=os.getpid(),
    )
    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=early_client,
            termination=current_process,
        )
    assert exc_info.value.code == 'origin_process_is_current_process'
    assert early_client.calls == []

    future_termination = replace(
        termination,
        origin_process_terminated_at=timezone.now() + timedelta(minutes=1),
    )
    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=early_client,
            termination=future_termination,
        )
    assert exc_info.value.code == 'termination_time_is_in_future'
    assert early_client.calls == []
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_client_must_explicitly_attest_authoritative_strong_absence():
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])
    client.authoritative_exact_key_version_listing = False

    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=termination,
        )

    attempt.refresh_from_db()
    assert (
        exc_info.value.code
        == 'authoritative_exact_version_listing_not_attested'
    )
    assert client.calls == []
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING


@pytest.mark.django_db(transaction=True)
def test_exploding_client_contract_property_is_redacted_before_snapshot_io():
    attempt, reference, termination = _pending_context()
    secret_detail = 'secret adapter property detail'

    class ExplodingPolicyClient:
        authoritative_exact_key_version_listing = True
        canary_policy_revision = 'canary-2026-08-20'

        def __init__(self):
            self.calls = 0

        @property
        def adapter_policy_revision(self):
            raise RuntimeError(secret_detail)

        def list_object_versions(self, **kwargs):
            self.calls += 1
            raise AssertionError('listing must not run')

    client = ExplodingPolicyClient()
    with (
        patch.object(
            reconciliation_module,
            '_load_pending_snapshot',
        ) as load_snapshot,
        pytest.raises(PutPendingReconciliationError) as exc_info,
    ):
        reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=termination,
        )

    assert (
        exc_info.value.code
        == 'authoritative_exact_version_client_unreadable'
    )
    assert secret_detail not in str(exc_info.value)
    load_snapshot.assert_not_called()
    assert client.calls == 0
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_client_policy_revision_drift_during_listing_creates_no_audit():
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])
    client.before_first_list = lambda: setattr(
        client,
        'canary_policy_revision',
        'different-canary-revision',
    )

    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=termination,
        )

    attempt.refresh_from_db()
    assert exc_info.value.code == 'version_listing_policy_changed'
    assert len(client.calls) == 1
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_exact_revision_cas_does_not_overwrite_concurrent_resolution():
    attempt, reference, termination = _pending_context()

    def resolve_elsewhere():
        version_known_at = timezone.now()
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=reference.expected_revision,
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            revision=F('revision') + 1,
            put_resolution_source=(
                MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
            ),
            object_version_id='direct-response-won-the-race',
            version_known_at=version_known_at,
            updated_at=version_known_at,
        )
        assert changed == 1

    client = FakeAuthoritativeVersionClient(
        [_page(attempt)],
        before_first_list=resolve_elsewhere,
    )

    result = reconcile_put_pending_upload_attempt(
        reference,
        client=client,
        termination=termination,
    )

    attempt.refresh_from_db()
    assert result.outcome == OUTCOME_SUPERSEDED
    assert result.applied is False
    assert (
        attempt.state
        == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
    )
    assert attempt.put_resolution_source == 'put_response'
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_reconciliation_refuses_an_outer_transaction_before_listing():
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])

    with pytest.raises(PutPendingReconciliationError) as exc_info:
        with transaction.atomic():
            reconcile_put_pending_upload_attempt(
                reference,
                client=client,
                termination=termination,
            )

    attempt.refresh_from_db()
    assert exc_info.value.code == 'reconciliation_inside_database_transaction'
    assert client.calls == []
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING


@pytest.mark.django_db(transaction=True)
def test_audit_insert_rolls_back_when_the_attempt_cas_does_not_apply():
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])
    original_update = QuerySet.update

    def fail_only_resolution_cas(queryset, **kwargs):
        if kwargs.get('put_resolution_source') == 'operator_reconciliation':
            return 0
        return original_update(queryset, **kwargs)

    with patch.object(
        QuerySet,
        'update',
        autospec=True,
        side_effect=fail_only_resolution_cas,
    ):
        with pytest.raises(PutPendingReconciliationError) as exc_info:
            reconcile_put_pending_upload_attempt(
                reference,
                client=client,
                termination=termination,
            )

    attempt.refresh_from_db()
    assert exc_info.value.code == 'attempt_resolution_cas_failed'
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    assert attempt.revision == reference.expected_revision
    assert attempt.put_resolution_source == ''
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ('field_name', 'invalid_value', 'error_code'),
    [
        ('evidence_digest', 'A' * 64, 'invalid_evidence_digest'),
        (
            'operator_identity_digest',
            'not-a-digest',
            'invalid_operator_identity_digest',
        ),
        (
            'origin_process_identity_digest',
            'f' * 63,
            'invalid_origin_process_identity_digest',
        ),
        (
            'digest_scheme_revision',
            'unsafe revision',
            'invalid_digest_scheme_revision',
        ),
        (
            'identity_digest_key_revision',
            'x' * 65,
            'invalid_identity_digest_key_revision',
        ),
    ],
)
def test_operator_audit_authorization_is_redacted_and_bounded(
    field_name,
    invalid_value,
    error_code,
):
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])

    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=replace(
                termination,
                **{field_name: invalid_value},
            ),
        )

    assert exc_info.value.code == error_code
    assert invalid_value not in str(exc_info.value)
    assert client.calls == []
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ('attribute', 'invalid_value', 'error_code'),
    [
        (
            'adapter_policy_revision',
            'adapter policy with spaces',
            'invalid_adapter_policy_revision',
        ),
        (
            'canary_policy_revision',
            'c' * 65,
            'invalid_canary_policy_revision',
        ),
    ],
)
def test_adapter_and_canary_policy_revisions_are_required_and_bounded(
    attribute,
    invalid_value,
    error_code,
):
    attempt, reference, termination = _pending_context()
    client = FakeAuthoritativeVersionClient([_page(attempt)])
    setattr(client, attribute, invalid_value)

    with pytest.raises(PutPendingReconciliationError) as exc_info:
        reconcile_put_pending_upload_attempt(
            reference,
            client=client,
            termination=termination,
        )

    assert exc_info.value.code == error_code
    assert invalid_value not in str(exc_info.value)
    assert client.calls == []
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()
