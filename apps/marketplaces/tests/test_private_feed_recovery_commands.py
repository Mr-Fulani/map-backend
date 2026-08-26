import io
import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def _reconciliation_args():
    return (
        '--tenant-id', '8',
        '--account-id', '4',
        '--endpoint-id', str(uuid.uuid4()),
        '--run-id', str(uuid.uuid4()),
        '--attempt-id', str(uuid.uuid4()),
        '--expected-attempt-revision', '1',
        '--origin-process-id', '1',
        '--origin-process-terminated-at', '2026-08-26T12:00:00+00:00',
        '--termination-evidence-reference', 'django-container-recreated-1',
        '--operator-reference', 'production-operator',
        '--origin-process-reference', 'old-container-pidns-init-1',
        '--identity-digest-key-revision', 'django-secret-key-2026-08',
        '--canary-policy-revision', 'account4-empty-list-2026-08-26-v1',
        '--apply',
        '--canary',
        '--confirm-account-id', '4',
        '--confirm-origin-process-terminated',
    )


def test_reconciliation_command_requires_origin_termination_confirmation():
    args = list(_reconciliation_args())
    args.remove('--confirm-origin-process-terminated')

    with pytest.raises(CommandError) as exc_info:
        call_command('reconcile_private_feed_artifact_put', *args)

    payload = json.loads(str(exc_info.value))
    assert payload['error_code'] == 'origin_termination_confirmation_required'


def test_reconciliation_command_outputs_only_redacted_result(settings):
    settings.SECRET_KEY = 'test-only-secret-key'
    output = io.StringIO()
    result = SimpleNamespace(
        attempt_id=uuid.uuid4(),
        outcome='no_object_by_reviewed_settlement_policy',
        state='no_object',
        revision=2,
        applied=True,
        pages_scanned=1,
        entries_scanned=0,
        exact_version_count=0,
        exact_delete_marker_count=0,
        settlement_remaining_seconds=0,
    )
    client = Mock()

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.private_feed_bucket_preflight',
        ) as preflight,
        patch(
            'apps.marketplaces.feed_artifact_clients.'
            'private_feed_authoritative_version_client',
            return_value=client,
        ) as client_factory,
        patch(
            'apps.marketplaces.feed_artifact_put_reconciliation.'
            'reconcile_put_pending_upload_attempt',
            return_value=result,
        ) as reconcile,
    ):
        call_command(
            'reconcile_private_feed_artifact_put',
            *_reconciliation_args(),
            stdout=output,
        )

    payload = json.loads(output.getvalue())
    assert payload == {
        'account_id': 4,
        'applied': True,
        'attempt_id': str(result.attempt_id),
        'entries_scanned': 0,
        'exact_delete_marker_count': 0,
        'exact_version_count': 0,
        'ok': True,
        'outcome': 'no_object_by_reviewed_settlement_policy',
        'pages_scanned': 1,
        'phase': 'reconcile_put',
        'revision': 2,
        'settlement_remaining_seconds': 0,
        'state': 'no_object',
    }
    rendered = output.getvalue()
    assert 'django-container-recreated-1' not in rendered
    assert 'production-operator' not in rendered
    assert 'old-container-pidns-init-1' not in rendered
    preflight.assert_called_once_with()
    client_factory.assert_called_once_with(
        canary_policy_revision='account4-empty-list-2026-08-26-v1',
    )
    termination = reconcile.call_args.kwargs['termination']
    assert termination.evidence_digest != 'django-container-recreated-1'
    assert len(termination.evidence_digest) == 64


@pytest.mark.django_db
def test_active_cutover_reconciliation_wakes_only_exact_account(settings):
    settings.SECRET_KEY = 'test-only-secret-key'
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'active'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'stable_bridge'
    settings.MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED = False
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = (4,)
    result = SimpleNamespace(
        attempt_id=uuid.uuid4(),
        outcome='no_object_by_reviewed_settlement_policy',
        state='no_object',
        revision=2,
        applied=True,
        pages_scanned=1,
        entries_scanned=0,
        exact_version_count=0,
        exact_delete_marker_count=0,
        settlement_remaining_seconds=0,
    )

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.private_feed_bucket_preflight',
        ),
        patch(
            'apps.marketplaces.feed_artifact_clients.'
            'private_feed_authoritative_version_client',
            return_value=Mock(),
        ),
        patch(
            'apps.marketplaces.feed_artifact_put_reconciliation.'
            'reconcile_put_pending_upload_attempt',
            return_value=result,
        ),
        patch(
            'apps.marketplaces.feed_intents.nudge_undispatched_feed_intent',
        ) as nudge,
    ):
        call_command(
            'reconcile_private_feed_artifact_put',
            *_reconciliation_args(),
            stdout=io.StringIO(),
        )

    assert nudge.call_args.args[0] == 4


def test_canary_resume_command_requires_all_exact_fences():
    with pytest.raises(CommandError) as exc_info:
        call_command(
            'canary_private_feed_artifact',
            '--account-id', '4',
            '--phase', 'resume',
            '--apply',
            '--canary',
            '--confirm-account-id', '4',
        )

    payload = json.loads(str(exc_info.value))
    assert payload['error_code'] == 'resume_fence_required'
