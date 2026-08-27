import uuid
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module

import pytest
from django.contrib import admin
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from apps.core.retention import purge_retained_data
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


@dataclass(frozen=True)
class AuditContext:
    tenant: Tenant
    account: MarketplaceAccount
    endpoint: MarketplaceFeedEndpoint
    run: MarketplaceFeedRun


def _context(slug: str) -> AuditContext:
    tenant = Tenant.objects.create(name=f'Audit {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Audit {slug}',
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
        claim_token=uuid.uuid4(),
        claimed_until=timezone.now() + timedelta(minutes=10),
    )
    return AuditContext(tenant=tenant, account=account, endpoint=endpoint, run=run)


def _pending(context: AuditContext) -> MarketplaceFeedArtifactUploadAttempt:
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.create(
        account=context.account,
        endpoint=context.endpoint,
        run=context.run,
        attempt_no=1,
        storage_bucket='private-feed-artifacts',
        expected_bucket_owner='cloud:owner/account-123',
        object_key=(
            f'private-feeds/v1/{context.endpoint.pk}/{context.run.pk}/'
            '00001/feed.xml'
        ),
        payload_sha256=PAYLOAD_DIGEST,
        size_bytes=1024,
        projection_count=3,
        content_type=MarketplaceFeedArtifact.CONTENT_TYPE_XML,
    )
    put_at = timezone.now() - timedelta(hours=1)
    # Construct a valid historical PUT_PENDING row without waiting through the
    # real 15-minute settlement window.  Production transitions still run
    # through the enabled trigger; only this disposable PostgreSQL fixture
    # temporarily suppresses user triggers while preserving CHECK constraints.
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
                put_at - timedelta(seconds=1),
                put_at,
                MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
                context.run.revision,
                put_at,
                attempt.pk,
            ],
        )
        assert cursor.rowcount == 1
    attempt.refresh_from_db()
    return attempt


def _audit_values(
    attempt: MarketplaceFeedArtifactUploadAttempt,
    *,
    to_state: str,
    decision_at=None,
    **overrides,
):
    decision_at = decision_at or timezone.now()
    values = {
        'attempt': attempt,
        'pre_revision': attempt.revision,
        'post_revision': attempt.revision + 1,
        'from_state': MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
        'to_state': to_state,
        'outcome': MarketplaceFeedPutReconciliationAudit.Outcome.MANUAL_REVIEW,
        'decision_code': 'put_reconcile_malformed_listing',
        'version_id_captured': False,
        'origin_process_identity_digest': 'c' * 64,
        'operator_identity_digest': 'd' * 64,
        'evidence_digest': 'e' * 64,
        'digest_scheme_revision': 'sha256-v1',
        'identity_digest_key_revision': 'identity-key-v1',
        'adapter_policy_revision': 'exact-list-v1',
        'canary_policy_revision': 'versioned-bucket-v1',
        'origin_process_terminated_at': decision_at - timedelta(minutes=20),
        'reconciliation_started_at': decision_at - timedelta(minutes=2),
        'decision_at': decision_at,
        'settlement_window_seconds': 900,
        'pages_scanned': 1,
        'entries_scanned': 0,
        'exact_version_count': 0,
        'exact_delete_marker_count': 0,
    }
    if to_state == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT:
        values.update(
            outcome=(
                MarketplaceFeedPutReconciliationAudit.Outcome.
                NO_OBJECT_BY_REVIEWED_SETTLEMENT_POLICY
            ),
            decision_code='reviewed_settlement_no_object',
        )
    elif to_state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN:
        values.update(
            outcome=MarketplaceFeedPutReconciliationAudit.Outcome.VERSION_KNOWN,
            decision_code='',
            version_id_captured=True,
            entries_scanned=1,
            exact_version_count=1,
        )
    values.update(overrides)
    return values


def test_put_reconciliation_audit_model_is_redacted_and_bounded():
    attempt_field = MarketplaceFeedArtifactUploadAttempt._meta.get_field(
        'put_resolution_source',
    )
    assert attempt_field.max_length == 32
    assert attempt_field.default == ''
    assert set(dict(attempt_field.choices)) == {
        'put_response',
        'operator_reconciliation',
    }

    fields = {
        field.name: field
        for field in MarketplaceFeedPutReconciliationAudit._meta.fields
    }
    assert {
        'attempt', 'pre_revision', 'post_revision', 'from_state', 'to_state',
        'outcome', 'decision_code', 'version_id_captured',
        'origin_process_identity_digest', 'operator_identity_digest',
        'evidence_digest', 'digest_scheme_revision',
        'identity_digest_key_revision', 'adapter_policy_revision',
        'canary_policy_revision', 'origin_process_terminated_at',
        'reconciliation_started_at', 'decision_at',
        'settlement_window_seconds', 'pages_scanned', 'entries_scanned',
        'exact_version_count', 'exact_delete_marker_count', 'created_at',
    } <= set(fields)
    assert fields['attempt'].one_to_one is True
    assert fields['attempt'].remote_field.on_delete.__name__ == 'PROTECT'
    assert not {
        'tenant', 'account', 'endpoint', 'run', 'storage_bucket', 'object_key',
        'object_version_id', 'evidence_reference', 'origin_process_id',
    } & set(fields)
    assert {constraint.name for constraint in (
        MarketplaceFeedPutReconciliationAudit._meta.constraints
    )} == {
        'mkt_put_aud_pre_revision',
        'mkt_put_aud_revision_step',
        'mkt_put_aud_from_state',
        'mkt_put_aud_to_state',
        'mkt_put_aud_decision_bundle',
        'mkt_put_aud_digests',
        'mkt_put_aud_policy_tokens',
        'mkt_put_aud_bounded_counts',
        'mkt_put_aud_count_order',
        'mkt_put_aud_time_order',
    }


def test_put_reconciliation_audit_admin_is_read_only():
    model_admin = admin.site._registry[MarketplaceFeedPutReconciliationAudit]
    assert model_admin.has_add_permission(None) is False
    assert model_admin.has_change_permission(None) is False
    assert model_admin.has_delete_permission(None) is False
    assert 'origin_process_identity_digest' not in model_admin.list_display
    assert 'operator_identity_digest' not in model_admin.list_display
    assert 'evidence_digest' not in model_admin.list_display


@pytest.mark.django_db(transaction=True)
def test_direct_put_response_sets_source_without_operator_audit():
    attempt = _pending(_context('direct-response'))
    decision_at = timezone.now()
    changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
        pk=attempt.pk,
        revision=attempt.revision,
        state=MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING,
    ).update(
        state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        revision=attempt.revision + 1,
        put_resolution_source=(
            MarketplaceFeedArtifactUploadAttempt.ResolutionSource.PUT_RESPONSE
        ),
        object_version_id='version-from-put-response',
        version_known_at=decision_at,
        updated_at=decision_at,
    )
    assert changed == 1
    attempt.refresh_from_db()
    assert attempt.put_resolution_source == 'put_response'
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()

    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedPutReconciliationAudit.objects.create(
            **_audit_values(
                attempt,
                to_state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            ),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedArtifactUploadAttempt.objects.filter(pk=attempt.pk).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            revision=attempt.revision + 1,
            put_resolution_source=(
                MarketplaceFeedArtifactUploadAttempt.ResolutionSource.
                OPERATOR_RECONCILIATION
            ),
            verified_at=timezone.now(),
            updated_at=timezone.now(),
        )
    attempt.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN
    assert attempt.put_resolution_source == 'put_response'


@pytest.mark.django_db(transaction=True)
def test_pending_resolution_without_source_is_rejected():
    attempt = _pending(_context('source-required'))
    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            revision=attempt.revision + 1,
            object_version_id='unguarded-version',
            version_known_at=timezone.now(),
            updated_at=timezone.now(),
        )
    attempt.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    assert attempt.put_resolution_source == ''


@pytest.mark.django_db(transaction=True)
def test_account_retention_keeps_unknown_put_ledger(settings):
    settings.SOFT_DELETE_RETENTION_DAYS = 1
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    context = _context('account-delete-put-pending')
    MarketplaceAccount.objects.filter(pk=context.account.pk).update(
        feed_intent_dispatched_revision=1,
    )
    attempt = _pending(context)

    context.account.refresh_from_db()
    context.account.soft_delete()
    expired = timezone.now() - timedelta(days=2)
    MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
        deleted_at=expired,
    )

    result = purge_retained_data()

    assert result['marketplace_accounts'] == 0
    assert MarketplaceAccount.all_objects.filter(pk=context.account.pk).exists()
    retained = MarketplaceFeedArtifactUploadAttempt.objects.get(pk=attempt.pk)
    assert retained.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
    assert retained.object_version_id is None
    assert retained.put_started_at is not None


@pytest.mark.django_db(transaction=True)
def test_operator_audit_and_terminal_transition_commit_as_one_pair():
    attempt = _pending(_context('operator-no-object'))
    decision_at = timezone.now()
    with transaction.atomic():
        audit = MarketplaceFeedPutReconciliationAudit.objects.create(
            **_audit_values(
                attempt,
                to_state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
                decision_at=decision_at,
            ),
        )
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
            revision=attempt.revision + 1,
            put_resolution_source=(
                MarketplaceFeedArtifactUploadAttempt.ResolutionSource.
                OPERATOR_RECONCILIATION
            ),
            resolved_at=decision_at,
            safe_error_code='reviewed_settlement_no_object',
            updated_at=decision_at,
        )

    assert changed == 1
    attempt.refresh_from_db()
    audit.refresh_from_db()
    assert attempt.put_resolution_source == 'operator_reconciliation'
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT
    assert audit.post_revision == attempt.revision
    assert audit.attempt_id == attempt.pk
    assert audit.settlement_window_seconds == 900
    assert audit.exact_version_count == 0
    assert audit.exact_delete_marker_count == 0

    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedPutReconciliationAudit.objects.filter(pk=audit.pk).update(
            decision_code='put_reconcile_page_limit',
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        audit.delete()


@pytest.mark.django_db(transaction=True)
def test_audited_version_can_reach_verified_before_deferred_pair_check():
    attempt = _pending(_context('operator-version-descendant'))
    decision_at = timezone.now()
    verified_at = decision_at + timedelta(seconds=1)
    with transaction.atomic():
        audit = MarketplaceFeedPutReconciliationAudit.objects.create(
            **_audit_values(
                attempt,
                to_state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
                decision_at=decision_at,
            ),
        )
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
            revision=attempt.revision + 1,
            put_resolution_source=(
                MarketplaceFeedArtifactUploadAttempt.ResolutionSource.
                OPERATOR_RECONCILIATION
            ),
            object_version_id='operator-reconciled-version',
            version_known_at=decision_at,
            updated_at=decision_at,
        )
        assert changed == 1
        changed = MarketplaceFeedArtifactUploadAttempt.objects.filter(
            pk=attempt.pk,
            revision=attempt.revision + 1,
            state=MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN,
        ).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.VERIFIED,
            revision=attempt.revision + 2,
            verified_at=verified_at,
            updated_at=verified_at,
        )
        assert changed == 1

    attempt.refresh_from_db()
    audit.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.VERIFIED
    assert attempt.revision > audit.post_revision
    assert attempt.put_resolution_source == 'operator_reconciliation'
    assert audit.to_state == MarketplaceFeedArtifactUploadAttempt.State.VERSION_KNOWN


@pytest.mark.django_db(transaction=True)
def test_operator_audit_cannot_commit_without_matching_attempt_update():
    attempt = _pending(_context('audit-without-pair'))
    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedPutReconciliationAudit.objects.create(
            **_audit_values(
                attempt,
                to_state=MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
            ),
        )

    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()
    attempt.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING


@pytest.mark.django_db(transaction=True)
def test_operator_termination_evidence_cannot_predate_put_boundary():
    attempt = _pending(_context('termination-before-put'))
    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedPutReconciliationAudit.objects.create(
            **_audit_values(
                attempt,
                to_state=MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
                origin_process_terminated_at=(
                    attempt.put_started_at - timedelta(seconds=1)
                ),
            ),
        )

    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_operator_audit_rejects_short_settlement_and_future_timestamps():
    attempt = _pending(_context('settlement-wall-clock'))
    now = timezone.now()
    invalid_overrides = (
        {'settlement_window_seconds': 899},
        {
            'origin_process_terminated_at': now - timedelta(minutes=20),
            'reconciliation_started_at': now + timedelta(minutes=1),
            'decision_at': now + timedelta(minutes=2),
        },
        {
            'origin_process_terminated_at': now - timedelta(minutes=20),
            'reconciliation_started_at': now - timedelta(minutes=2),
            'decision_at': now + timedelta(minutes=1),
        },
    )

    for overrides in invalid_overrides:
        audit_overrides = dict(overrides)
        decision_at = audit_overrides.pop('decision_at', now)
        with pytest.raises(IntegrityError), transaction.atomic():
            MarketplaceFeedPutReconciliationAudit.objects.create(
                **_audit_values(
                    attempt,
                    to_state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
                    decision_at=decision_at,
                    **audit_overrides,
                ),
            )
            MarketplaceFeedArtifactUploadAttempt.objects.filter(
                pk=attempt.pk,
                revision=attempt.revision,
            ).update(
                state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
                revision=attempt.revision + 1,
                put_resolution_source='operator_reconciliation',
                resolved_at=decision_at,
                safe_error_code='reviewed_settlement_no_object',
                updated_at=decision_at,
            )

        assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
            attempt=attempt,
        ).exists()
        attempt.refresh_from_db()
        assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING
        assert attempt.put_resolution_source == ''


@pytest.mark.django_db(transaction=True)
def test_mismatched_operator_decision_rolls_back_audit_and_attempt():
    attempt = _pending(_context('audit-mismatch'))
    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceFeedPutReconciliationAudit.objects.create(
            **_audit_values(
                attempt,
                to_state=MarketplaceFeedArtifactUploadAttempt.State.NO_OBJECT,
            ),
        )
        MarketplaceFeedArtifactUploadAttempt.objects.filter(pk=attempt.pk).update(
            state=MarketplaceFeedArtifactUploadAttempt.State.MANUAL_REVIEW,
            revision=attempt.revision + 1,
            put_resolution_source='operator_reconciliation',
            resolved_at=timezone.now(),
            safe_error_code='put_reconcile_malformed_listing',
            updated_at=timezone.now(),
        )

    assert not MarketplaceFeedPutReconciliationAudit.objects.filter(
        attempt=attempt,
    ).exists()
    attempt.refresh_from_db()
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.PUT_PENDING


def test_put_audit_migrations_are_fail_closed_and_lock_in_live_write_order():
    expand = import_module(
        'apps.marketplaces.migrations.0029_private_feed_artifacts',
    )
    guards = import_module(
        'apps.marketplaces.migrations.0030_private_feed_artifact_guards',
    )

    assert expand.Migration.atomic is True
    assert ('marketplaces', '0028_feed_run_source_intent_unique') in (
        expand.Migration.dependencies
    )

    assert guards.Migration.atomic is True
    assert guards.Migration.dependencies == [
        ('marketplaces', '0029_private_feed_artifacts'),
    ]
    operation = guards.Migration.operations[0]
    assert isinstance(operation, migrations.RunSQL)
    assert 'BEFORE INSERT OR UPDATE OR DELETE' in operation.sql
    assert 'DEFERRABLE INITIALLY DEFERRED' in operation.sql
    assert 'feed_put_audit_guard_preflight_failed' in operation.sql
    assert 'feed_put_audit_guard_reverse_preflight_failed' in operation.reverse_sql
    assert 'put_resolution_source' in operation.sql
    assert 'object_version_id IS NOT NULL' in operation.sql
    audit_insert_guard = operation.sql.split(
        'CREATE OR REPLACE FUNCTION mkt_feed_put_audit_guard_fn()',
        1,
    )[1].split(
        'CREATE OR REPLACE FUNCTION mkt_feed_put_resolution_source_guard_fn()',
        1,
    )[0]
    assert 'FOR SHARE' not in audit_insert_guard
    assert 'attempt_row.put_started_at IS NULL' in audit_insert_guard
    assert 'NEW.origin_process_terminated_at < attempt_row.put_started_at' in (
        audit_insert_guard
    )
    assert 'NEW.settlement_window_seconds IS DISTINCT FROM 900' in (
        audit_insert_guard
    )
    assert "interval '900 seconds'" in audit_insert_guard
    assert 'NEW.reconciliation_started_at > observed_at' in audit_insert_guard
    assert 'NEW.decision_at > observed_at' in audit_insert_guard

    audit_lock_orders = (
        guards.FORWARD_SQL.split(
            '-- 0034 and 0035 commit independently.',
            1,
        )[1].split(
            'CREATE OR REPLACE FUNCTION mkt_feed_put_audit_guard_fn()',
            1,
        )[0],
        guards.REVERSE_SQL.split(
            '-- Keep the provenance guards installed after any use.',
            1,
        )[1].split(
            'DROP TRIGGER IF EXISTS mkt_feed_put_audit_pair_deferred_trg',
            1,
        )[0],
    )
    for sql in audit_lock_orders:
        artifact = sql.index('marketplaces_marketplacefeedartifact,')
        audit = sql.index(
            'marketplaces_marketplacefeedputreconciliationaudit,',
        )
        attempt = sql.index(
            'marketplaces_marketplacefeedartifactuploadattempt,',
        )
        run = sql.index('marketplaces_marketplacefeedrun,')
        endpoint = sql.index('marketplaces_marketplacefeedendpoint')
        assert artifact < audit < attempt < run < endpoint


@pytest.mark.django_db
def test_put_audit_live_model_matches_guarded_migration_state():
    loader = MigrationLoader(connection)
    state = loader.project_state([
        ('marketplaces', '0030_private_feed_artifact_guards'),
    ])

    for live_model in (
        MarketplaceFeedArtifactUploadAttempt,
        MarketplaceFeedPutReconciliationAudit,
    ):
        historical = state.apps.get_model('marketplaces', live_model.__name__)
        assert {field.name for field in historical._meta.fields} == {
            field.name for field in live_model._meta.fields
        }
        assert {constraint.name for constraint in historical._meta.constraints} == {
            constraint.name for constraint in live_model._meta.constraints
        }


@pytest.mark.django_db
def test_put_audit_triggers_are_installed_and_deferred_in_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL trigger catalog contract')

    expected = {
        'mkt_feed_put_audit_guard_trg': (False, False),
        'mkt_feed_put_resolution_source_guard_trg': (False, False),
        'mkt_feed_put_audit_pair_deferred_trg': (True, True),
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
