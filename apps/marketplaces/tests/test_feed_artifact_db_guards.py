import uuid
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module

import pytest
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.core.retention import purge_retained_data
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedFetchEvidence,
    MarketplaceFeedRun,
)
from apps.tenants.models import Tenant


OWNER_DIGEST = 'a' * 64
PAYLOAD_DIGEST = 'b' * 64


@dataclass(frozen=True)
class GuardContext:
    tenant: Tenant
    account: MarketplaceAccount
    endpoint: MarketplaceFeedEndpoint
    run: MarketplaceFeedRun
    upload_attempt: int = 1


def _context(
    slug,
    *,
    endpoint_source=1,
    run_source=1,
    run_endpoint_revision=0,
    predecessor_artifact_id=None,
    upload_attempt=1,
    run_payload=PAYLOAD_DIGEST,
    run_owner_digest=OWNER_DIGEST,
):
    tenant = Tenant.objects.create(name=f'Guard {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Guard {slug}',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-test-credentials',
        feed_intent_revision=endpoint_source,
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=OWNER_DIGEST,
        source_intent_revision=endpoint_source,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
    )
    run = MarketplaceFeedRun.objects.create(
        tenant=tenant,
        account=account,
        marketplace=account.marketplace,
        account_identity_digest=run_owner_digest,
        payload_sha256=run_payload,
        source_intent_revision=run_source,
        endpoint_revision=run_endpoint_revision,
        predecessor_artifact_id=predecessor_artifact_id,
        claim_token=uuid.uuid4(),
        claimed_until=timezone.now() + timedelta(minutes=10),
    )
    return GuardContext(
        tenant=tenant,
        account=account,
        endpoint=endpoint,
        run=run,
        upload_attempt=upload_attempt,
    )


def _artifact_values(context, **overrides):
    attempt = overrides.get('upload_attempt', context.upload_attempt)
    values = {
        'endpoint': context.endpoint,
        'account': context.account,
        'run': context.run,
        'upload_attempt': attempt,
        'storage_bucket': 'private-feed-artifacts',
        'object_key': (
            f'private-feeds/v1/{context.endpoint.pk}/{context.run.pk}/'
            f'{attempt:05d}/feed.xml'
        ),
        'object_version_id': f'version-{uuid.uuid4()}',
        'payload_sha256': context.run.payload_sha256,
        'size_bytes': 1024,
        'listing_count': 1,
        'content_type': MarketplaceFeedArtifact.CONTENT_TYPE_XML,
        'verification_method': (
            MarketplaceFeedArtifact.VERIFICATION_VERSION_READBACK_SHA256
        ),
        'verified_at': timezone.now(),
    }
    values.update(overrides)
    return values


def _artifact(context, **overrides):
    values = _artifact_values(context, **overrides)
    ledger = _verified_ledger(context, artifact_values=values)
    attached_at = timezone.now()
    with transaction.atomic():
        artifact = MarketplaceFeedArtifact.objects.create(**values)
        changed = MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            feed_artifact=artifact,
            artifact_upload_attempt=artifact.upload_attempt,
        )
        assert changed == 1
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=ledger.pk,
            revision=3,
            state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
            revision=4,
            attached_at=attached_at,
            resolved_at=attached_at,
            updated_at=attached_at,
        )
        assert changed == 1
    context.run.refresh_from_db()
    return artifact


def _verified_ledger(context, *, artifact_values=None):
    values = artifact_values or _artifact_values(context)
    ledger = MarketplaceFeedArtifactUploadAttempt.objects.create(
        account=context.account,
        endpoint=context.endpoint,
        run=context.run,
        attempt_no=values['upload_attempt'],
        storage_bucket=values['storage_bucket'],
        expected_bucket_owner='cloud:owner/account-123',
        object_key=values['object_key'],
        payload_sha256=values['payload_sha256'],
        size_bytes=values['size_bytes'],
        projection_count=values['listing_count'],
        content_type=values['content_type'],
    )
    transition_at = timezone.now()
    # ``artifact_values`` is assembled before the ledger row is inserted.
    # Bind the artifact timestamp to the actual ledger transition so the
    # strict database time-order constraint is deterministic on PostgreSQL.
    values['verified_at'] = transition_at
    changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
        pk=ledger.pk,
        revision=0,
    ).update(
        state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        revision=1,
        put_run_revision=context.run.revision,
        put_started_at=transition_at,
        updated_at=transition_at,
    )
    assert changed == 1
    changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
        pk=ledger.pk,
        revision=1,
    ).update(
        state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        revision=2,
        put_resolution_source=(
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
        ),
        object_version_id=values['object_version_id'],
        version_known_at=transition_at,
        updated_at=transition_at,
    )
    assert changed == 1
    changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
        pk=ledger.pk,
        revision=2,
    ).update(
        state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
        revision=3,
        verified_at=values['verified_at'],
        updated_at=transition_at,
    )
    assert changed == 1
    ledger.refresh_from_db()
    return ledger


def _select_artifact(context, artifact):
    context.run.refresh_from_db()
    assert context.run.feed_artifact_id == artifact.pk
    assert context.run.artifact_upload_attempt == artifact.upload_attempt


def _promote(context, artifact):
    promoted_at = timezone.now()
    changed = MarketplaceFeedEndpoint.objects.filter(
        pk=context.endpoint.pk,
    ).update(
        current_artifact=artifact,
        artifact_revision=context.endpoint.artifact_revision + 1,
        source_intent_revision=context.run.source_intent_revision,
        artifact_promoted_at=promoted_at,
    )
    assert changed == 1
    context.endpoint.refresh_from_db()
    return promoted_at


def _evidence_values(context, artifact, **overrides):
    values = {
        'endpoint': context.endpoint,
        'artifact': artifact,
        'request_method': MarketplaceFeedFetchEvidence.RequestMethod.GET,
        'accepted_token_key_id': context.endpoint.token_key_id,
        'capability_revision': context.endpoint.capability_revision,
        'endpoint_revision': context.endpoint.artifact_revision,
        'source_intent_revision': context.run.source_intent_revision,
        'run_revision': context.run.revision,
        'redirect_expires_at': timezone.now() + timedelta(seconds=60),
    }
    values.update(overrides)
    return values


def _promoted_evidence_at(
    slug,
    *,
    issued_at,
    redirect_ttl_seconds=60,
):
    context = _context(slug)
    artifact = _artifact(context)
    _select_artifact(context, artifact)
    _promote(context, artifact)
    evidence = MarketplaceFeedFetchEvidence.objects.create(
        **_evidence_values(
            context,
            artifact,
            redirect_expires_at=(
                issued_at + timedelta(seconds=redirect_ttl_seconds)
            ),
        ),
    )
    return context, artifact, evidence


def _rejects_write(callback):
    with pytest.raises(IntegrityError), transaction.atomic():
        callback()


@pytest.mark.django_db
def test_exact_attached_artifact_is_immutable():
    context = _context('artifact-guard-exact')
    artifact = _artifact(context)

    assert artifact.object_key.endswith('/00001/feed.xml')

    def update_artifact():
        MarketplaceFeedArtifact.objects.filter(pk=artifact.pk).update(size_bytes=2048)

    _rejects_write(update_artifact)


@pytest.mark.django_db
@pytest.mark.parametrize(
    'case',
    (
        'stale_source',
        'stale_predecessor',
        'payload',
        'attempt',
        'object_key',
        'owner_identity',
        'inactive_account',
        'inactive_tenant',
    ),
)
def test_artifact_insert_rejects_stale_or_unowned_snapshots(case):
    context = _context(
        f'artifact-guard-{case}',
        predecessor_artifact_id=(uuid.uuid4() if case == 'stale_predecessor' else None),
        run_owner_digest=('c' * 64 if case == 'owner_identity' else OWNER_DIGEST),
    )
    overrides = {}
    if case == 'stale_source':
        with transaction.atomic():
            MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
                feed_intent_revision=2,
            )
            MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
                source_intent_revision=2,
            )
    elif case == 'payload':
        overrides['payload_sha256'] = 'd' * 64
    elif case == 'attempt':
        overrides['upload_attempt'] = 2
    elif case == 'object_key':
        overrides['object_key'] = (
            f'private-feeds/v1/{context.endpoint.pk}/{context.run.pk}/'
            '00001/not-feed.xml'
        )
    elif case == 'inactive_account':
        MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(is_active=False)
    elif case == 'inactive_tenant':
        Tenant.objects.filter(pk=context.tenant.pk).update(is_active=False)

    _rejects_write(
        lambda: MarketplaceFeedArtifact.objects.create(
            **_artifact_values(context, **overrides),
        ),
    )


@pytest.mark.django_db
def test_artifact_insert_rejects_cross_account_ownership():
    context = _context('artifact-guard-owner-a')
    other = _context('artifact-guard-owner-b')
    values = _artifact_values(context)
    values['account'] = other.account

    _rejects_write(lambda: MarketplaceFeedArtifact.objects.create(**values))


@pytest.mark.django_db
def test_run_artifact_pointer_is_exact_and_one_way():
    context = _context('run-artifact-guard-a')
    artifact = _artifact(context)
    other = _context('run-artifact-guard-b')
    other_artifact = _artifact(other)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            feed_artifact=other_artifact,
        ),
    )

    _select_artifact(context, artifact)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            feed_artifact=None,
        ),
    )

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            artifact_upload_attempt=2,
        ),
    )


@pytest.mark.django_db
def test_artifact_selection_cannot_mutate_run_provenance_in_same_update():
    context = _context('run-artifact-selection-sealed')
    artifact = _artifact(context)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            feed_artifact=artifact,
            source_intent_revision=2,
        ),
    )

    context.run.refresh_from_db()
    assert context.run.feed_artifact_id == artifact.pk
    assert context.run.source_intent_revision == 1


@pytest.mark.django_db
def test_verified_ledger_seals_run_snapshot_even_before_artifact_insert():
    context = _context('run-artifact-preselection-sealed')
    _verified_ledger(context)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            source_intent_revision=2,
        ),
    )

    context.run.refresh_from_db()
    assert context.run.feed_artifact_id is None
    assert context.run.source_intent_revision == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    'mutation',
    (
        {'account_identity_digest': 'c' * 64},
        {'source_intent_revision': 2},
        {'endpoint_revision': 2},
        {'predecessor_artifact_id': uuid.UUID('00000000-0000-0000-0000-000000000001')},
        {'artifact_upload_attempt': 2},
        {'payload_sha256': 'c' * 64},
    ),
)
def test_selected_run_snapshot_is_sealed_while_lifecycle_fields_remain_mutable(
    mutation,
):
    context = _context(f'selected-run-sealed-{next(iter(mutation))}')
    artifact = _artifact(context)
    _select_artifact(context, artifact)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            **mutation,
        ),
    )

    changed = MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
        state=MarketplaceFeedRun.State.SUCCEEDED,
        revision=2,
        finished_at=timezone.now(),
    )
    assert changed == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    'mutation',
    (
        {'payload_sha256': 'c' * 64},
        {'artifact_upload_attempt': 2},
    ),
)
def test_verified_artifact_prevents_snapshot_drift_before_selection(mutation):
    context = _context(f'run-artifact-drift-{next(iter(mutation))}')
    artifact = _artifact(context)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(**mutation),
    )
    _select_artifact(context, artifact)


@pytest.mark.django_db
def test_endpoint_promotion_is_exact_and_pointer_metadata_is_one_way():
    context = _context('endpoint-artifact-guard')
    artifact = _artifact(context)
    _select_artifact(context, artifact)

    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            artifact_revision=1,
        ),
    )

    promoted_at = _promote(context, artifact)
    assert context.endpoint.current_artifact_id == artifact.pk
    assert context.endpoint.artifact_revision == 1
    assert context.endpoint.artifact_promoted_at == promoted_at

    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            current_artifact=None,
            artifact_revision=0,
            artifact_promoted_at=None,
        ),
    )
    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            artifact_promoted_at=timezone.now() + timedelta(seconds=1),
        ),
    )


@pytest.mark.django_db
def test_endpoint_owner_is_immutable_even_before_artifact_promotion():
    context = _context('endpoint-owner-sealed')
    other = _context('endpoint-owner-sealed-other')

    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            account=other.account,
        ),
    )


@pytest.mark.django_db
def test_dark_private_endpoint_can_be_disarmed_before_promotion():
    context = _context('endpoint-dark-disarm')

    updated = MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
        storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
    )

    assert updated == 1
    context.endpoint.refresh_from_db()
    assert context.endpoint.storage_mode == MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
    assert context.endpoint.serve_enabled is False
    assert context.endpoint.current_artifact_id is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    'mutation',
    (
        {'owner_identity_digest': 'c' * 64},
        {'capability_revision': 2},
        {'token_key_id': 'feed-hmac-v2'},
        {'storage_mode': MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE},
        {'profile_revision': 2},
    ),
)
def test_endpoint_promotion_cannot_smuggle_profile_or_capability_mutation(mutation):
    context = _context(f'endpoint-promotion-config-{next(iter(mutation))}')
    artifact = _artifact(context)
    _select_artifact(context, artifact)

    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            current_artifact=artifact,
            artifact_revision=1,
            source_intent_revision=1,
            artifact_promoted_at=timezone.now(),
            **mutation,
        ),
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'mutation',
    (
        {'source_intent_revision': 2},
        {'endpoint_revision': 1},
        {'predecessor_artifact_id': uuid.UUID('00000000-0000-0000-0000-000000000001')},
        {'payload_sha256': 'c' * 64},
        {'artifact_upload_attempt': 2},
    ),
)
def test_selected_run_seal_prevents_snapshot_drift_before_promotion(mutation):
    context = _context(f'endpoint-run-drift-{next(iter(mutation))}')
    artifact = _artifact(context)
    _select_artifact(context, artifact)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(**mutation),
    )
    _promote(context, artifact)


@pytest.mark.django_db
def test_endpoint_rejects_stale_source_and_cross_endpoint_promotions():
    stale = _context('endpoint-artifact-stale')
    stale_artifact = _artifact(stale)
    _select_artifact(stale, stale_artifact)
    with transaction.atomic():
        MarketplaceAccount.all_objects.filter(pk=stale.account.pk).update(
            feed_intent_revision=2,
        )
        MarketplaceFeedEndpoint.objects.filter(pk=stale.endpoint.pk).update(
            source_intent_revision=2,
        )
    stale.endpoint.refresh_from_db()

    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=stale.endpoint.pk).update(
            current_artifact=stale_artifact,
            artifact_revision=1,
            source_intent_revision=2,
            artifact_promoted_at=timezone.now(),
        ),
    )

    first = _context('endpoint-artifact-cross-a')
    second = _context('endpoint-artifact-cross-b')
    second_artifact = _artifact(second)
    _select_artifact(second, second_artifact)
    _rejects_write(
        lambda: MarketplaceFeedEndpoint.objects.filter(pk=first.endpoint.pk).update(
            current_artifact=second_artifact,
            artifact_revision=1,
            source_intent_revision=1,
            artifact_promoted_at=timezone.now(),
        ),
    )


@pytest.mark.django_db
def test_endpoint_allows_exact_next_generation_replacement():
    context = _context('endpoint-artifact-next')
    first_artifact = _artifact(context)
    _select_artifact(context, first_artifact)
    _promote(context, first_artifact)

    MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
        state=MarketplaceFeedRun.State.SUCCEEDED,
    )
    with transaction.atomic():
        MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
            feed_intent_revision=2,
        )
        MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            source_intent_revision=2,
        )
    context.endpoint.refresh_from_db()
    second_run = MarketplaceFeedRun.objects.create(
        tenant=context.tenant,
        account=context.account,
        marketplace=context.account.marketplace,
        account_identity_digest=OWNER_DIGEST,
        payload_sha256='c' * 64,
        source_intent_revision=2,
        endpoint_revision=1,
        predecessor_artifact_id=first_artifact.pk,
        claim_token=uuid.uuid4(),
        claimed_until=timezone.now() + timedelta(minutes=10),
    )
    second_context = GuardContext(
        tenant=context.tenant,
        account=context.account,
        endpoint=context.endpoint,
        run=second_run,
        upload_attempt=1,
    )
    second_artifact = _artifact(second_context, payload_sha256='c' * 64)
    _select_artifact(second_context, second_artifact)
    _promote(second_context, second_artifact)

    assert second_context.endpoint.current_artifact_id == second_artifact.pk
    assert second_context.endpoint.artifact_revision == 2
    assert second_context.endpoint.source_intent_revision == 2


@pytest.mark.django_db
def test_fetch_evidence_accepts_exact_snapshot_and_rejects_stale_values():
    context = _context('fetch-evidence-guard')
    artifact = _artifact(context)
    _select_artifact(context, artifact)
    _promote(context, artifact)

    evidence = MarketplaceFeedFetchEvidence.objects.create(
        **_evidence_values(context, artifact),
    )
    assert evidence.redirect_status == 307

    invalid_overrides = (
        {'accepted_token_key_id': 'feed-hmac-v2'},
        {'capability_revision': context.endpoint.capability_revision + 1},
        {'endpoint_revision': context.endpoint.artifact_revision + 1},
        {'source_intent_revision': context.endpoint.source_intent_revision + 1},
        {'run_revision': context.run.revision + 1},
    )
    for overrides in invalid_overrides:
        _rejects_write(
            lambda overrides=overrides: MarketplaceFeedFetchEvidence.objects.create(
                **_evidence_values(context, artifact, **overrides),
            ),
        )

    _rejects_write(
        lambda: MarketplaceFeedFetchEvidence.objects.filter(pk=evidence.pk).update(
            request_method=MarketplaceFeedFetchEvidence.RequestMethod.HEAD,
        ),
    )

    MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
        previous_token_key_id='feed-hmac-v0',
        profile_state=MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        legacy_object_key='legacy/feed.xml',
        legacy_profile_url='https://storage.example.test/legacy/feed.xml',
        profile_fingerprint='d' * 64,
        profile_verified_at=timezone.now(),
    )
    context.endpoint.refresh_from_db()
    previous_key_evidence = MarketplaceFeedFetchEvidence.objects.create(
        **_evidence_values(
            context,
            artifact,
            accepted_token_key_id='feed-hmac-v0',
        ),
    )
    assert previous_key_evidence.accepted_token_key_id == 'feed-hmac-v0'


@pytest.mark.django_db
def test_fetch_evidence_keeps_serving_verified_artifact_during_next_build():
    context = _context('fetch-evidence-next-build')
    artifact = _artifact(context)
    _select_artifact(context, artifact)
    _promote(context, artifact)

    with transaction.atomic():
        MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
            feed_intent_revision=2,
        )
        MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            source_intent_revision=2,
        )
    context.account.refresh_from_db()
    context.endpoint.refresh_from_db()

    evidence = MarketplaceFeedFetchEvidence.objects.create(
        **_evidence_values(context, artifact),
    )
    assert evidence.source_intent_revision == 1
    assert context.endpoint.source_intent_revision == 2

    _rejects_write(
        lambda: MarketplaceFeedFetchEvidence.objects.create(
            **_evidence_values(context, artifact, source_intent_revision=2),
        ),
    )


@pytest.mark.django_db
def test_fetch_evidence_rejects_cross_pointer_and_inactive_owner():
    context = _context('fetch-evidence-owner-a')
    artifact = _artifact(context)
    _select_artifact(context, artifact)
    _promote(context, artifact)

    other = _context('fetch-evidence-owner-b')
    other_artifact = _artifact(other)
    _select_artifact(other, other_artifact)
    _promote(other, other_artifact)

    _rejects_write(
        lambda: MarketplaceFeedFetchEvidence.objects.create(
            **_evidence_values(context, other_artifact),
        ),
    )

    MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(is_active=False)
    _rejects_write(
        lambda: MarketplaceFeedFetchEvidence.objects.create(
            **_evidence_values(context, artifact),
        ),
    )

    tenant_context = _context('fetch-evidence-tenant-inactive')
    tenant_artifact = _artifact(tenant_context)
    _select_artifact(tenant_context, tenant_artifact)
    _promote(tenant_context, tenant_artifact)
    Tenant.objects.filter(pk=tenant_context.tenant.pk).update(is_active=False)
    _rejects_write(
        lambda: MarketplaceFeedFetchEvidence.objects.create(
            **_evidence_values(tenant_context, tenant_artifact),
        ),
    )


@pytest.mark.django_db
def test_guard_migration_is_reversible_state_free_trigger_ddl():
    loader = MigrationLoader(connection)
    matches = [
        migration
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == 'marketplaces'
        and name == '0030_private_feed_artifact_guards'
    ]
    assert len(matches) == 1
    migration = matches[0]
    assert migration.atomic is True
    assert migration.dependencies == [
        ('marketplaces', '0029_private_feed_artifacts'),
    ]
    assert len(migration.operations) == 1
    operation = migration.operations[0]
    assert isinstance(operation, migrations.RunSQL)

    function_names = {
        'mkt_feed_artifact_guard_fn',
        'mkt_feed_endpoint_art_guard_fn',
        'mkt_feed_run_art_guard_fn',
        'mkt_feed_fetch_guard_fn',
    }
    trigger_names = {
        'mkt_feed_artifact_guard_trg',
        'mkt_feed_endpoint_art_guard_trg',
        'mkt_feed_run_art_guard_trg',
        'mkt_feed_fetch_guard_trg',
    }
    for name in function_names:
        assert f'FUNCTION {name}()' in operation.sql
        assert f'DROP FUNCTION IF EXISTS {name}()' in operation.reverse_sql
    for name in trigger_names:
        assert f'CREATE TRIGGER {name}' in operation.sql
        assert f'DROP TRIGGER IF EXISTS {name}' in operation.reverse_sql

    assert 'CREATE INDEX' not in operation.sql
    assert 'ALTER TABLE' not in operation.sql
    assert 'RunPython' not in operation.sql


@pytest.mark.django_db
def test_guard_sql_never_inverts_account_endpoint_or_tenant_lock_order():
    """Row triggers must not acquire a parent lock after the target UPDATE."""

    migration_module = import_module(
        'apps.marketplaces.migrations.0030_private_feed_artifact_guards',
    )
    sql = migration_module.FORWARD_SQL
    endpoint_guard = sql.split(
        'CREATE OR REPLACE FUNCTION mkt_feed_endpoint_art_guard_fn()',
        1,
    )[1].split(
        'CREATE OR REPLACE FUNCTION mkt_feed_run_art_guard_fn()',
        1,
    )[0]
    run_guard = sql.split(
        'CREATE OR REPLACE FUNCTION mkt_feed_run_art_guard_fn()',
        1,
    )[1].split(
        'CREATE OR REPLACE FUNCTION mkt_feed_fetch_guard_fn()',
        1,
    )[0]
    artifact_guard = sql.split(
        'CREATE OR REPLACE FUNCTION mkt_feed_artifact_guard_fn()',
        1,
    )[1].split(
        'CREATE OR REPLACE FUNCTION mkt_feed_endpoint_art_guard_fn()',
        1,
    )[0]
    fetch_guard = sql.split(
        'CREATE OR REPLACE FUNCTION mkt_feed_fetch_guard_fn()',
        1,
    )[1]

    # Endpoint/run UPDATE statements already hold their own row lock before
    # BEFORE UPDATE executes. Parent/child locks here would invert the
    # application account -> endpoint -> run protocol.
    assert 'FOR UPDATE' not in endpoint_guard
    assert 'FOR SHARE' not in endpoint_guard
    assert 'FOR UPDATE' not in run_guard
    assert 'FOR SHARE' not in run_guard

    # INSERT guards may acquire canonical owner locks, but Tenant is only a
    # liveness snapshot and must never be locked last.
    artifact_tenant_read = artifact_guard.split('FROM tenants_tenant', 1)[1]
    fetch_tenant_read = fetch_guard.split('FROM tenants_tenant', 1)[1]
    assert 'FOR UPDATE' not in artifact_tenant_read.split(';', 1)[0]
    assert 'FOR SHARE' not in artifact_tenant_read.split(';', 1)[0]
    assert 'FOR UPDATE' not in fetch_tenant_read.split(';', 1)[0]
    assert 'FOR SHARE' not in fetch_tenant_read.split(';', 1)[0]


@pytest.mark.django_db
def test_all_four_guards_are_installed_and_enabled_in_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL trigger catalog contract')

    expected_triggers = {
        'mkt_feed_artifact_guard_trg',
        'mkt_feed_endpoint_art_guard_trg',
        'mkt_feed_run_art_guard_trg',
        'mkt_feed_fetch_guard_trg',
    }
    expected_functions = {
        'mkt_feed_artifact_guard_fn',
        'mkt_feed_endpoint_art_guard_fn',
        'mkt_feed_run_art_guard_fn',
        'mkt_feed_fetch_guard_fn',
    }
    with connection.cursor() as cursor:
        cursor.execute(
            '''
            SELECT trigger_name, event_manipulation
              FROM information_schema.triggers
             WHERE trigger_schema = current_schema()
               AND trigger_name = ANY(%s)
            ''',
            [list(expected_triggers)],
        )
        trigger_events = {}
        for name, event in cursor.fetchall():
            trigger_events.setdefault(name, set()).add(event)

        cursor.execute(
            '''
            SELECT proname
              FROM pg_proc
              JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
             WHERE pg_namespace.nspname = current_schema()
               AND proname = ANY(%s)
            ''',
            [list(expected_functions)],
        )
        functions = {name for (name,) in cursor.fetchall()}

    assert set(trigger_events) == expected_triggers
    assert trigger_events['mkt_feed_artifact_guard_trg'] == {'INSERT', 'UPDATE'}
    assert trigger_events['mkt_feed_endpoint_art_guard_trg'] == {'UPDATE'}
    assert trigger_events['mkt_feed_run_art_guard_trg'] == {'INSERT', 'UPDATE'}
    assert trigger_events['mkt_feed_fetch_guard_trg'] == {'INSERT', 'UPDATE'}
    assert functions == expected_functions


@pytest.mark.django_db(transaction=True)
def test_guard_reverse_sql_removes_and_forward_sql_restores_all_objects():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL trigger catalog contract')

    migration_module = import_module(
        'apps.marketplaces.migrations.0030_private_feed_artifact_guards',
    )
    trigger_names = [
        'mkt_feed_artifact_guard_trg',
        'mkt_feed_endpoint_art_guard_trg',
        'mkt_feed_run_art_guard_trg',
        'mkt_feed_fetch_guard_trg',
    ]
    function_names = [name.replace('_trg', '_fn') for name in trigger_names]

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(migration_module.REVERSE_SQL)
        try:
            cursor.execute(
                '''
                SELECT count(*)
                  FROM information_schema.triggers
                 WHERE trigger_schema = current_schema()
                   AND trigger_name = ANY(%s)
                ''',
                [trigger_names],
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                '''
                SELECT count(*)
                  FROM pg_proc
                  JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                 WHERE pg_namespace.nspname = current_schema()
                   AND proname = ANY(%s)
                ''',
                [function_names],
            )
            assert cursor.fetchone()[0] == 0
        finally:
            cursor.execute(migration_module.FORWARD_SQL)

        cursor.execute(
            '''
            SELECT count(DISTINCT trigger_name)
              FROM information_schema.triggers
             WHERE trigger_schema = current_schema()
               AND trigger_name = ANY(%s)
            ''',
            [trigger_names],
        )
        assert cursor.fetchone()[0] == len(trigger_names)


@pytest.mark.django_db
def test_account_retention_preserves_current_exact_version_and_evidence(settings):
    settings.SOFT_DELETE_RETENTION_DAYS = 1
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    context = _context('account-delete-current-artifact')
    MarketplaceAccount.objects.filter(pk=context.account.pk).update(
        feed_intent_dispatched_revision=1,
    )
    artifact = _artifact(context)
    _select_artifact(context, artifact)
    _promote(context, artifact)
    evidence = MarketplaceFeedFetchEvidence.objects.create(
        **_evidence_values(context, artifact),
    )

    context.account.refresh_from_db()
    context.account.soft_delete()
    expired = timezone.now() - timedelta(days=2)
    MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
        deleted_at=expired,
    )

    dry_run = purge_retained_data(dry_run=True)
    applied = purge_retained_data()

    assert dry_run['marketplace_accounts'] == 0
    assert applied['marketplace_accounts'] == 0
    retained_account = MarketplaceAccount.all_objects.get(pk=context.account.pk)
    retained_endpoint = MarketplaceFeedEndpoint.objects.get(pk=context.endpoint.pk)
    retained_artifact = MarketplaceFeedArtifact.objects.get(pk=artifact.pk)
    retained_attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(
        run=context.run,
    )
    assert retained_account.deleted_at == expired
    assert retained_endpoint.current_artifact_id == artifact.pk
    assert retained_artifact.object_version_id == artifact.object_version_id
    assert retained_attempt.object_version_id == artifact.object_version_id
    assert MarketplaceFeedFetchEvidence.objects.filter(pk=evidence.pk).exists()

    with pytest.raises(ProtectedError), transaction.atomic():
        retained_account.hard_delete()


@pytest.mark.django_db
@pytest.mark.skip(reason='P7 retention/GC remains frozen')
def test_fetch_evidence_retention_is_expired_resolved_and_retry_fenced(
    settings,
    monkeypatch,
):
    from apps.core.models import BackgroundJobDispatch
    from apps.core.retention import purge_retained_data

    settings.MARKETPLACE_FEED_FETCH_EVIDENCE_RETENTION_DAYS = 30
    settings.BACKGROUND_JOB_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    current_time = timezone.now()
    clock = {'now': current_time - timedelta(days=31)}
    monkeypatch.setattr(timezone, 'now', lambda: clock['now'])

    expired_context, _, expired = _promoted_evidence_at(
        'evidence-retention-expired',
        issued_at=clock['now'],
    )
    MarketplaceFeedRun.objects.filter(pk=expired_context.run.pk).update(
        state=MarketplaceFeedRun.State.SUCCEEDED,
        finished_at=clock['now'],
    )

    uncertain_context, _, uncertain = _promoted_evidence_at(
        'evidence-retention-uncertain',
        issued_at=clock['now'],
    )
    MarketplaceFeedRun.objects.filter(pk=uncertain_context.run.pk).update(
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        finished_at=clock['now'],
    )

    retry_context, _, retryable = _promoted_evidence_at(
        'evidence-retention-retryable',
        issued_at=clock['now'],
    )
    MarketplaceFeedRun.objects.filter(pk=retry_context.run.pk).update(
        state=MarketplaceFeedRun.State.FAILED,
        finished_at=clock['now'],
    )
    retry_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.marketplaces.tasks.process_marketplace_feed_run_step',
        queue='avito_publish',
        args=[str(retry_context.run.pk), retry_context.run.revision],
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=1,
        max_run_attempts=3,
        # This deliberately crosses generic dispatch retention too. The
        # retry fence must survive one pass in order to protect evidence on
        # every later pass until the delivery is truly exhausted.
        finished_at=clock['now'],
    )
    malformed_revision_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.marketplaces.tasks.process_marketplace_feed_run_step',
        queue='avito_publish',
        args=[str(retry_context.run.pk), 'not-an-integer'],
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=3,
        max_run_attempts=3,
        finished_at=clock['now'],
    )
    malformed_run_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.marketplaces.tasks.process_marketplace_feed_run_step',
        queue='avito_publish',
        args=['not-a-uuid', retry_context.run.revision],
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=3,
        max_run_attempts=3,
        finished_at=clock['now'],
    )
    malformed_dispatch_ids = [
        malformed_revision_dispatch.pk,
        malformed_run_dispatch.pk,
    ]

    active_context, _, active = _promoted_evidence_at(
        'evidence-retention-active',
        issued_at=clock['now'],
    )
    assert active_context.run.state == MarketplaceFeedRun.State.PREPARING

    clock['now'] = current_time - timedelta(days=29)
    fresh_context, _, fresh = _promoted_evidence_at(
        'evidence-retention-fresh',
        issued_at=clock['now'],
    )
    MarketplaceFeedRun.objects.filter(pk=fresh_context.run.pk).update(
        state=MarketplaceFeedRun.State.SUCCEEDED,
        finished_at=clock['now'],
    )
    clock['now'] = current_time

    dry_run = purge_retained_data(dry_run=True)

    assert dry_run['marketplace_feed_fetch_evidence'] == 1
    assert MarketplaceFeedFetchEvidence.objects.filter(pk=expired.pk).exists()

    first = purge_retained_data()

    assert first['marketplace_feed_fetch_evidence'] == 1
    assert not MarketplaceFeedFetchEvidence.objects.filter(pk=expired.pk).exists()
    for retained in (uncertain, retryable, active, fresh):
        assert MarketplaceFeedFetchEvidence.objects.filter(pk=retained.pk).exists()
    assert BackgroundJobDispatch.objects.filter(pk=retry_dispatch.pk).exists()
    assert BackgroundJobDispatch.objects.filter(
        pk__in=malformed_dispatch_ids,
    ).count() == 2

    repeated = purge_retained_data()

    assert repeated['marketplace_feed_fetch_evidence'] == 0
    assert MarketplaceFeedFetchEvidence.objects.filter(pk=retryable.pk).exists()
    assert BackgroundJobDispatch.objects.filter(pk=retry_dispatch.pk).exists()
    assert BackgroundJobDispatch.objects.filter(
        pk__in=malformed_dispatch_ids,
    ).count() == 2

    BackgroundJobDispatch.objects.filter(pk=retry_dispatch.pk).update(
        run_attempts=3,
    )
    released = purge_retained_data()

    assert released['marketplace_feed_fetch_evidence'] == 1
    assert not MarketplaceFeedFetchEvidence.objects.filter(pk=retryable.pk).exists()
    assert not BackgroundJobDispatch.objects.filter(pk=retry_dispatch.pk).exists()
    assert BackgroundJobDispatch.objects.filter(
        pk__in=malformed_dispatch_ids,
    ).count() == 2
    for retained in (uncertain, active, fresh):
        assert MarketplaceFeedFetchEvidence.objects.filter(pk=retained.pk).exists()


@pytest.mark.django_db
@pytest.mark.skip(reason='P7 retention/GC remains frozen')
def test_fetch_evidence_retention_never_deletes_live_redirect_and_is_bounded(
    settings,
    monkeypatch,
):
    from apps.core.retention import purge_retained_data

    current_time = timezone.now()
    clock = {'now': current_time - timedelta(days=31)}
    monkeypatch.setattr(timezone, 'now', lambda: clock['now'])
    settings.MARKETPLACE_FEED_FETCH_EVIDENCE_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 1

    expired_rows = []
    for suffix in ('one', 'two'):
        context, _, evidence = _promoted_evidence_at(
            f'evidence-retention-bounded-{suffix}',
            issued_at=clock['now'],
        )
        MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            state=MarketplaceFeedRun.State.SUCCEEDED,
            finished_at=clock['now'],
        )
        expired_rows.append(evidence)

    # The second expiry fence is independent of the audit cutoff. Tests may
    # override the already startup-validated setting to expose that boundary.
    settings.MARKETPLACE_FEED_FETCH_EVIDENCE_RETENTION_DAYS = 0
    clock['now'] = current_time - timedelta(seconds=60)
    live_context, _, live_redirect = _promoted_evidence_at(
        'evidence-retention-live-redirect',
        issued_at=clock['now'],
        redirect_ttl_seconds=120,
    )
    MarketplaceFeedRun.objects.filter(pk=live_context.run.pk).update(
        state=MarketplaceFeedRun.State.SUCCEEDED,
        finished_at=clock['now'],
    )
    clock['now'] = current_time

    first = purge_retained_data()
    assert first['marketplace_feed_fetch_evidence'] == 1
    assert MarketplaceFeedFetchEvidence.objects.filter(
        pk=live_redirect.pk,
    ).exists()

    second = purge_retained_data()
    assert second['marketplace_feed_fetch_evidence'] == 1
    assert not MarketplaceFeedFetchEvidence.objects.filter(
        pk__in=[row.pk for row in expired_rows],
    ).exists()
    assert MarketplaceFeedFetchEvidence.objects.filter(
        pk=live_redirect.pk,
    ).exists()
