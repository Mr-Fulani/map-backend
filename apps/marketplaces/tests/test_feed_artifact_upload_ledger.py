import uuid
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module

import pytest
from django.contrib import admin
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)
from apps.tenants.models import Tenant


OWNER_DIGEST = 'a' * 64
PAYLOAD_DIGEST = 'b' * 64
BUCKET = 'private-feed-artifacts'
BUCKET_OWNER = 'cloud:owner/account-123'


@dataclass(frozen=True)
class UploadContext:
    tenant: Tenant
    account: MarketplaceAccount
    endpoint: MarketplaceFeedEndpoint
    run: MarketplaceFeedRun


def _context(slug, *, total_count=999):
    tenant = Tenant.objects.create(name=f'Upload {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Upload {slug}',
        external_id=f'{slug}-external',
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
        predecessor_artifact_id=None,
        total_count=total_count,
        claim_token=uuid.uuid4(),
        claimed_until=timezone.now() + timedelta(minutes=10),
    )
    return UploadContext(
        tenant=tenant,
        account=account,
        endpoint=endpoint,
        run=run,
    )


def _attempt_values(context, *, attempt_no=1, projection_count=3, **overrides):
    values = {
        'account': context.account,
        'endpoint': context.endpoint,
        'run': context.run,
        'attempt_no': attempt_no,
        'storage_bucket': BUCKET,
        'expected_bucket_owner': BUCKET_OWNER,
        'object_key': (
            f'private-feeds/v1/{context.endpoint.pk}/{context.run.pk}/'
            f'{attempt_no:05d}/feed.xml'
        ),
        'payload_sha256': PAYLOAD_DIGEST,
        'size_bytes': 1024,
        'projection_count': projection_count,
        'content_type': MarketplaceFeedArtifact.CONTENT_TYPE_XML,
    }
    values.update(overrides)
    return values


def _prepared(context, *, attempt_no=1, projection_count=3, **overrides):
    return MarketplaceFeedArtifactUploadAttempt.objects.create(
        **_attempt_values(
            context,
            attempt_no=attempt_no,
            projection_count=projection_count,
            **overrides,
        ),
    )


def _transition(attempt, state, **fields):
    changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
        pk=attempt.pk,
        revision=attempt.revision,
        state=attempt.state,
    ).update(
        state=state,
        revision=attempt.revision + 1,
        updated_at=timezone.now(),
        **fields,
    )
    assert changed == 1
    attempt.refresh_from_db()
    return attempt


def _verified(context, *, attempt_no=1, projection_count=3):
    attempt = _prepared(
        context,
        attempt_no=attempt_no,
        projection_count=projection_count,
    )
    put_at = timezone.now()
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        put_run_revision=context.run.revision,
        put_started_at=put_at,
    )
    version_at = timezone.now()
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        put_resolution_source=(
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
        ),
        object_version_id=f'version-{uuid.uuid4()}',
        version_known_at=version_at,
    )
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
        verified_at=timezone.now(),
    )
    return attempt


def _artifact_values(context, attempt, **overrides):
    values = {
        'endpoint': context.endpoint,
        'account': context.account,
        'run': context.run,
        'upload_attempt': attempt.attempt_no,
        'storage_bucket': attempt.storage_bucket,
        'object_key': attempt.object_key,
        'object_version_id': attempt.object_version_id,
        'payload_sha256': attempt.payload_sha256,
        'size_bytes': attempt.size_bytes,
        'listing_count': attempt.projection_count,
        'content_type': attempt.content_type,
        'verification_method': (
            MarketplaceFeedArtifact.VERIFICATION_VERSION_READBACK_SHA256
        ),
        'verified_at': attempt.verified_at,
    }
    values.update(overrides)
    return values


def _attach(context, attempt):
    attached_at = timezone.now()
    with transaction.atomic():
        artifact = MarketplaceFeedArtifact.objects.create(
            **_artifact_values(context, attempt),
        )
        changed = MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            feed_artifact=artifact,
            artifact_upload_attempt=attempt.attempt_no,
        )
        assert changed == 1
        _transition(
            attempt,
            MarketplaceFeedArtifactUploadAttempt.State.ATTACHED,
            attached_at=attached_at,
            resolved_at=attached_at,
        )
    context.run.refresh_from_db()
    return artifact


def _rejects_write(callback):
    with pytest.raises(IntegrityError), transaction.atomic():
        callback()


@pytest.mark.django_db
def test_prepared_snapshot_is_dark_exact_and_does_not_use_run_total_count():
    context = _context('ledger-prepared', total_count=999)
    attempt = _prepared(context, projection_count=3)

    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PREPARED
    assert attempt.revision == 0
    assert attempt.projection_count == 3
    assert context.run.total_count == 999
    assert context.run.artifact_upload_attempt == 0
    assert context.run.feed_artifact_id is None
    assert attempt.object_key.endswith('/00001/feed.xml')


@pytest.mark.django_db
def test_exact_state_machine_attaches_all_three_records_atomically():
    context = _context('ledger-attached')
    attempt = _verified(context, projection_count=3)
    artifact = _attach(context, attempt)

    attempt.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
    assert attempt.revision == 4
    assert attempt.attached_at is not None
    assert attempt.resolved_at is not None
    assert context.run.feed_artifact_id == artifact.pk
    assert context.run.artifact_upload_attempt == 1
    assert artifact.listing_count == 3


@pytest.mark.django_db
def test_prepared_can_close_without_claiming_that_put_started():
    context = _context('ledger-abandoned')
    first = _prepared(context)
    resolved_at = timezone.now()
    _transition(
        first,
        MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
        resolved_at=resolved_at,
        safe_error_code='abandoned_before_put',
    )

    assert first.put_started_at is None
    assert first.put_run_revision is None
    assert first.object_version_id is None
    assert first.resolved_at == resolved_at

    _rejects_write(
        lambda: _prepared(context, attempt_no=2, projection_count=4),
    )
    second = _prepared(context, attempt_no=2)
    assert second.attempt_no == 2


@pytest.mark.django_db
def test_put_pending_cannot_close_no_object_without_operator_audit():
    context = _context('ledger-no-object')
    attempt = _prepared(context)
    put_at = timezone.now()
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        put_run_revision=context.run.revision,
        put_started_at=put_at,
    )
    _rejects_write(
        lambda: _transition(
            attempt,
            MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
            resolved_at=timezone.now(),
            safe_error_code='object_absence_verified',
        ),
    )

    attempt.refresh_from_db()
    assert attempt.put_started_at == put_at
    assert attempt.object_version_id is None
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING


@pytest.mark.django_db
def test_verified_attempt_can_be_marked_orphaned_but_terminal_is_immutable():
    context = _context('ledger-orphan')
    attempt = _verified(context)
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.ORPHANED,
        resolved_at=timezone.now(),
        safe_error_code='stale_attachment_fence',
    )

    assert attempt.object_version_id
    assert attempt.verified_at is not None
    _rejects_write(
        lambda: MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
            revision=attempt.revision + 1,
            safe_error_code='operator_review',
        ),
    )
    _rejects_write(lambda: _prepared(context, attempt_no=2))


@pytest.mark.django_db
def test_upload_ledger_rows_cannot_be_deleted_without_future_gc_intent():
    context = _context('ledger-delete')
    attempt = _prepared(context)

    _rejects_write(lambda: attempt.delete())
    assert MarketplaceFeedArtifactUploadAttempt.objects.filter(pk=attempt.pk).exists()


@pytest.mark.django_db
def test_attempt_sequence_locator_and_snapshot_are_guarded():
    context = _context('ledger-guards')
    first = _prepared(context)

    _rejects_write(lambda: _prepared(context, attempt_no=2))
    _rejects_write(
        lambda: MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=first.pk,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            revision=2,
            put_run_revision=context.run.revision,
            put_started_at=timezone.now(),
        ),
    )
    _rejects_write(
        lambda: MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=first.pk,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            revision=1,
            put_run_revision=context.run.revision,
            put_started_at=timezone.now(),
            expected_bucket_owner='different-owner',
        ),
    )


@pytest.mark.django_db
def test_run_upload_attempt_cannot_be_reserved_before_atomic_attachment():
    context = _context('ledger-run-zero')
    _prepared(context)

    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            artifact_upload_attempt=1,
        ),
    )
    _rejects_write(
        lambda: MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
            source_intent_revision=2,
        ),
    )


@pytest.mark.django_db
def test_live_claim_and_active_tenant_are_required_at_external_boundaries():
    expired = _context('ledger-expired-insert')
    MarketplaceFeedRun.objects.filter(pk=expired.run.pk).update(
        claimed_until=timezone.now() - timedelta(seconds=1),
    )
    _rejects_write(lambda: _prepared(expired))

    inactive = _context('ledger-inactive-put')
    attempt = _prepared(inactive)
    Tenant.objects.filter(pk=inactive.tenant.pk).update(is_active=False)
    _rejects_write(
        lambda: _transition(
            attempt,
            MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
            put_run_revision=inactive.run.revision,
            put_started_at=timezone.now(),
        ),
    )


@pytest.mark.django_db
def test_exact_version_is_captured_after_put_lease_expires():
    context = _context('ledger-version-after-expiry')
    attempt = _prepared(context)
    put_revision = context.run.revision
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        put_run_revision=put_revision,
        put_started_at=timezone.now(),
    )
    MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
        claimed_until=timezone.now() - timedelta(seconds=1),
    )

    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        put_resolution_source=(
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
        ),
        object_version_id='version-returned-after-expiry',
        version_known_at=timezone.now(),
    )

    assert attempt.object_version_id == 'version-returned-after-expiry'
    assert attempt.put_run_revision == put_revision
    _rejects_write(
        lambda: _transition(
            attempt,
            MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            verified_at=timezone.now(),
        ),
    )
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.ORPHANED,
        resolved_at=timezone.now(),
        safe_error_code='stale_generation',
    )
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ORPHANED


@pytest.mark.django_db
def test_exact_version_capture_ignores_owner_availability_then_renews_claim():
    context = _context('ledger-version-owner-drift')
    attempt = _prepared(context)
    put_revision = context.run.revision
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        put_run_revision=put_revision,
        put_started_at=timezone.now(),
    )
    MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
        is_active=False,
    )
    Tenant.objects.filter(pk=context.tenant.pk).update(is_active=False)

    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        put_resolution_source=(
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
        ),
        object_version_id='version-returned-after-owner-disabled',
        version_known_at=timezone.now(),
    )
    _rejects_write(
        lambda: _transition(
            attempt,
            MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            verified_at=timezone.now(),
        ),
    )

    MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
        is_active=True,
    )
    Tenant.objects.filter(pk=context.tenant.pk).update(is_active=True)
    renewed_until = timezone.now() + timedelta(minutes=10)
    MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
        revision=put_revision + 1,
        claim_token=uuid.uuid4(),
        claimed_until=renewed_until,
    )
    _transition(
        attempt,
        MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
        verified_at=timezone.now(),
    )
    artifact = _attach(context, attempt)

    context.run.refresh_from_db()
    assert context.run.revision == put_revision + 1
    assert attempt.put_run_revision == put_revision
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED
    assert context.run.feed_artifact_id == artifact.pk


@pytest.mark.django_db(transaction=True)
def test_artifact_insert_requires_still_live_claim_and_seals_preselection_snapshot():
    context = _context('ledger-artifact-claim')
    attempt = _verified(context)
    MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
        claimed_until=timezone.now() - timedelta(seconds=1),
    )
    _rejects_write(
        lambda: MarketplaceFeedArtifact.objects.create(
            **_artifact_values(context, attempt),
        ),
    )

    MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
        claimed_until=timezone.now() + timedelta(minutes=10),
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MarketplaceFeedArtifact.objects.create(
                **_artifact_values(context, attempt),
            )
            MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
                source_intent_revision=2,
            )

    context.run.refresh_from_db()
    assert context.run.source_intent_revision == 1
    assert not MarketplaceFeedArtifact.objects.filter(run=context.run).exists()


@pytest.mark.django_db(transaction=True)
def test_deferred_guard_rejects_artifact_insert_without_pointer_and_attached_ledger():
    context = _context('ledger-deferred-artifact')
    attempt = _verified(context)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MarketplaceFeedArtifact.objects.create(
                **_artifact_values(context, attempt),
            )

    assert not MarketplaceFeedArtifact.objects.filter(run=context.run).exists()
    attempt.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERIFIED


@pytest.mark.django_db(transaction=True)
def test_deferred_guard_rejects_pointer_when_attached_transition_is_omitted():
    context = _context('ledger-deferred-pointer')
    attempt = _verified(context)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            artifact = MarketplaceFeedArtifact.objects.create(
                **_artifact_values(context, attempt),
            )
            MarketplaceFeedRun.objects.filter(pk=context.run.pk).update(
                feed_artifact=artifact,
                artifact_upload_attempt=attempt.attempt_no,
            )

    context.run.refresh_from_db()
    assert context.run.feed_artifact_id is None
    assert context.run.artifact_upload_attempt == 0
    assert not MarketplaceFeedArtifact.objects.filter(run=context.run).exists()


@pytest.mark.django_db
def test_upload_migrations_are_additive_and_fail_closed_on_reverse():
    loader = MigrationLoader(connection)
    expand = loader.disk_migrations[
        ('marketplaces', '0029_private_feed_artifacts')
    ]
    guards = loader.disk_migrations[
        ('marketplaces', '0030_private_feed_artifact_guards')
    ]

    assert expand.atomic is True
    assert ('marketplaces', '0028_feed_run_source_intent_unique') in (
        expand.dependencies
    )
    assert isinstance(expand.operations[0], migrations.CreateModel)

    assert guards.atomic is True
    assert guards.dependencies == [
        ('marketplaces', '0029_private_feed_artifacts'),
    ]
    assert len(guards.operations) == 1
    operation = guards.operations[0]
    assert isinstance(operation, migrations.RunSQL)
    assert 'DEFERRABLE INITIALLY DEFERRED' in operation.sql
    assert 'run_row.total_count' not in operation.sql
    assert 'ledger_row.projection_count' in operation.sql
    assert 'expected_bucket_owner' in operation.sql
    assert 'BEFORE INSERT OR UPDATE OR DELETE' in operation.sql
    assert 'ledger_exists' in operation.sql
    assert 'clock_timestamp()' in operation.sql
    assert 'CURRENT_TIMESTAMP' not in operation.sql
    assert 'feed_upload_ledger_preflight_failed' in operation.sql
    assert 'IN SHARE ROW EXCLUSIVE MODE' in operation.sql
    assert 'FROM marketplaces_marketplacefeedartifactuploadattempt' in operation.sql
    assert 'WHERE feed_artifact_id IS NOT NULL' in operation.sql
    assert 'WHERE artifact_upload_attempt <> 0' in operation.sql
    assert 'WHERE current_artifact_id IS NOT NULL' in operation.sql
    assert 'CREATE INDEX' not in operation.sql
    assert 'ALTER TABLE' not in operation.sql
    assert 'DROP FUNCTION IF EXISTS mkt_feed_upload_guard_fn()' in operation.reverse_sql
    assert 'feed_upload_ledger_reverse_preflight_failed' in operation.reverse_sql
    assert 'IN SHARE ROW EXCLUSIVE MODE' in operation.reverse_sql
    assert "OLD.state = 'put_pending' AND NEW.state = 'version_known'" in operation.sql
    assert 'feed_upload_version_snapshot_rejected' in operation.sql
    assert 'run_row.revision < NEW.put_run_revision' in operation.sql


@pytest.mark.django_db
def test_upload_guard_sql_preserves_parent_lock_order():
    migration_module = import_module(
        'apps.marketplaces.migrations.0030_private_feed_artifact_guards',
    )
    sql = migration_module.FORWARD_SQL
    upload_guard = sql.split(
        'CREATE OR REPLACE FUNCTION mkt_feed_upload_guard_fn()',
        1,
    )[1].split(
        'CREATE OR REPLACE FUNCTION mkt_feed_artifact_guard_fn()',
        1,
    )[0]
    update_reads = upload_guard.split(
        '-- Updates execute after the application has acquired the canonical parent',
        1,
    )[1]

    account_lock = upload_guard.index('FROM marketplaces_marketplaceaccount')
    endpoint_lock = upload_guard.index('FROM marketplaces_marketplacefeedendpoint')
    run_lock = upload_guard.index('FROM marketplaces_marketplacefeedrun')
    assert account_lock < endpoint_lock < run_lock
    assert 'FOR UPDATE' not in update_reads
    assert 'FOR SHARE' not in update_reads


@pytest.mark.django_db
def test_upload_preflight_table_locks_follow_attachment_write_order():
    migration_module = import_module(
        'apps.marketplaces.migrations.0030_private_feed_artifact_guards',
    )
    preflights = (
        migration_module.FORWARD_SQL.split(
            '-- There is no safe way to infer an exact, pre-PUT ledger snapshot',
            1,
        )[1].split(
            'CREATE OR REPLACE FUNCTION mkt_feed_upload_guard_fn()',
            1,
        )[0],
        migration_module.REVERSE_SQL.split(
            '-- A downgrade cannot preserve the pre-PUT journal contract',
            1,
        )[1].split(
            'DROP TRIGGER IF EXISTS mkt_feed_upload_attach_deferred_trg',
            1,
        )[0],
    )

    for preflight in preflights:
        artifact_lock = preflight.index(
            'marketplaces_marketplacefeedartifact,',
        )
        run_lock = preflight.index('marketplaces_marketplacefeedrun,')
        ledger_lock = preflight.index(
            'marketplaces_marketplacefeedartifactuploadattempt,',
        )
        endpoint_lock = preflight.index(
            'marketplaces_marketplacefeedendpoint',
        )
        assert artifact_lock < run_lock < ledger_lock < endpoint_lock


@pytest.mark.django_db
def test_upload_guards_are_installed_and_deferred_in_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL trigger catalog contract')

    expected = {
        'mkt_feed_upload_guard_trg': (False, False),
        'mkt_feed_artifact_attach_deferred_trg': (True, True),
        'mkt_feed_run_attach_deferred_trg': (True, True),
        'mkt_feed_upload_attach_deferred_trg': (True, True),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            '''
            SELECT tgname, tgdeferrable, tginitdeferred
              FROM pg_trigger
              JOIN pg_class ON pg_class.oid = pg_trigger.tgrelid
              JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
             WHERE pg_namespace.nspname = current_schema()
               AND NOT tgisinternal
               AND tgname = ANY(%s)
            ''',
            [list(expected)],
        )
        installed = {
            name: (is_deferred, initially_deferred)
            for name, is_deferred, initially_deferred in cursor.fetchall()
        }

    assert installed == expected


def test_upload_attempt_admin_is_read_only_and_hides_raw_locator():
    model_admin = admin.site._registry[MarketplaceFeedArtifactUploadAttempt]
    hidden = {
        'storage_bucket',
        'expected_bucket_owner',
        'object_key',
        'object_version_id',
    }

    assert hidden <= set(model_admin.exclude)
    assert hidden.isdisjoint(model_admin.list_display)
    assert hidden.isdisjoint(model_admin.search_fields)
    assert hidden.isdisjoint(model_admin.readonly_fields)
    assert model_admin.has_add_permission(None) is False
    assert model_admin.has_change_permission(None) is False
    assert model_admin.has_delete_permission(None) is False
