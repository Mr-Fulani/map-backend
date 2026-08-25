import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.marketplaces.management.commands.migrate_marketplace_feed_profile import (
    Command,
    _json_command_error,
)


SOURCE_FINGERPRINT = 'a' * 64
TARGET_FINGERPRINT = 'b' * 64


def _result(*, phase='inspect', dry_run=True, state='bridge_ready'):
    return SimpleNamespace(
        phase=phase,
        dry_run=dry_run,
        account_id=22,
        tenant_id=11,
        state=state,
        revision=7,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=TARGET_FINGERPRINT,
        verification_outcome='source',
        owned_feed_count=1,
        foreign_feed_count=2,
        parity_verified=True,
        settlement_remaining_seconds=0,
        # These fields intentionally look useful but must never reach stdout.
        stable_url='https://feeds.example.test/feed.xml?id=secret&key=token',
        legacy_profile_url='https://storage.example.test/private/feed.xml',
        report_email='operator@example.test',
        provider_profile={'feeds_data': [{'feed_url': 'https://secret.test'}]},
    )


def _command(
    *,
    tenant_id=11,
    account_id=22,
    phase='inspect',
    apply=False,
    expected_revision=None,
    expected_source_fingerprint=None,
    canary=False,
    confirm_account_id=None,
):
    stdout = StringIO()
    call_command(
        'migrate_marketplace_feed_profile',
        tenant_id=tenant_id,
        account_id=account_id,
        phase=phase,
        expected_revision=expected_revision,
        expected_source_fingerprint=expected_source_fingerprint,
        apply=apply,
        canary=canary,
        confirm_account_id=confirm_account_id,
        stdout=stdout,
    )
    return json.loads(stdout.getvalue())


@pytest.mark.parametrize(
    'arguments',
    [
        ['--account-id', '22', '--phase', 'inspect'],
        ['--tenant-id', '11', '--phase', 'inspect'],
        [
            '--tenant-id', '0', '--account-id', '22', '--phase', 'inspect',
        ],
        [
            '--tenant-id', '11', '--account-id', '-1', '--phase', 'inspect',
        ],
        [
            '--tenant-id', '11', '--account-id', '22', '--phase', 'inspect',
            '--expected-revision', str(1 << 63),
        ],
        [
            '--tenant-id', '11', '--account-id', '22', '--phase', 'inspect',
            '--expected-source-fingerprint', 'A' * 64,
        ],
    ],
)
def test_parser_requires_exact_bounded_account_scope(arguments):
    parser = Command().create_parser(
        'manage.py',
        'migrate_marketplace_feed_profile',
    )

    with pytest.raises(CommandError):
        parser.parse_args(arguments)


def test_canary_flag_is_documented_as_acknowledgement_not_provider_proof():
    parser = Command().create_parser(
        'manage.py',
        'migrate_marketplace_feed_profile',
    )
    canary_action = next(
        action for action in parser._actions
        if '--canary' in action.option_strings
    )

    assert 'acknowledgement' in canary_action.help
    assert 'not proof' in canary_action.help
    assert 'HTTP 307' in canary_action.help


def test_non_string_core_error_code_cannot_break_redacted_json():
    class ErrorCode:
        def __str__(self):
            return 'profile_conflict'

    detail = json.loads(str(_json_command_error(
        ErrorCode(),
        'Feed-profile migration was refused.',
    )))

    assert detail['error_code'] == 'profile_conflict'


@pytest.mark.parametrize(
    ('options', 'error_code'),
    [
        ({'tenant_id': '11'}, 'invalid_scope'),
        ({'expected_revision': 1 << 63}, 'invalid_expected_revision'),
        (
            {'expected_source_fingerprint': 'A' * 64},
            'invalid_expected_source_fingerprint',
        ),
        ({'confirm_account_id': '22'}, 'invalid_account_confirmation'),
        ({'apply': 'false'}, 'invalid_confirmation_flags'),
    ],
)
def test_programmatic_call_command_cannot_bypass_argparse_fences(options, error_code):
    with pytest.raises(CommandError) as error:
        _command(**options)

    detail = json.loads(str(error.value))
    assert detail['error_code'] == error_code


@pytest.mark.parametrize(
    'options',
    [
        {'account_id': 0},
        {'phase': 'unknown'},
    ],
)
def test_django_parser_rejects_invalid_required_kwargs_before_handle(options):
    with pytest.raises(CommandError) as error:
        _command(**options)

    rendered = str(error.value)
    assert 'https://' not in rendered
    assert 'key=' not in rendered
    assert '@' not in rendered


@override_settings(MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=False)
def test_default_is_scoped_dry_run_and_output_is_strictly_redacted():
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        return_value=_result(),
    ) as migrate:
        summary = _command()

    assert summary == {
        'ok': True,
        'phase': 'inspect',
        'dry_run': True,
        'account_id': 22,
        'tenant_id': 11,
        'state': 'bridge_ready',
        'revision': 7,
        'source_fingerprint': SOURCE_FINGERPRINT,
        'target_fingerprint': TARGET_FINGERPRINT,
        'verification_outcome': 'source',
        'owned_feed_count': 1,
        'foreign_feed_count': 2,
        'parity_verified': True,
        'settlement_remaining_seconds': 0,
    }
    encoded = json.dumps(summary, sort_keys=True)
    assert 'url' not in encoded
    assert 'email' not in encoded
    assert 'feeds_data' not in encoded
    assert 'secret' not in encoded
    migrate.assert_called_once_with(
        tenant_id=11,
        account_id=22,
        phase='inspect',
        expected_revision=None,
        expected_source_fingerprint=None,
        apply=False,
    )


@pytest.mark.parametrize(
    'phase',
    [
        'prepare',
        'migrate',
        'reconcile',
        'resolve-source',
        'confirm-prepare-source',
    ],
)
@override_settings(MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=False)
def test_every_phase_can_be_rehearsed_without_mutation_when_gate_is_off(phase):
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        return_value=_result(phase=phase),
    ) as migrate:
        summary = _command(phase=phase)

    assert summary['phase'] == phase
    assert summary['dry_run'] is True
    migrate.assert_called_once_with(
        tenant_id=11,
        account_id=22,
        phase=phase,
        expected_revision=None,
        expected_source_fingerprint=None,
        apply=False,
    )


@pytest.mark.parametrize('phase', ['prepare', 'migrate'])
@override_settings(MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=False)
def test_new_migration_admission_is_fail_closed_even_with_all_confirmations(phase):
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
    ) as migrate, pytest.raises(CommandError) as error:
        _command(
            phase=phase,
            apply=True,
            expected_revision=7,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
            canary=True,
            confirm_account_id=22,
        )

    detail = json.loads(str(error.value))
    assert detail['error_code'] == 'migration_admission_disabled'
    migrate.assert_not_called()


@pytest.mark.parametrize('phase', ['prepare', 'migrate'])
@override_settings(MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=True)
def test_enabled_admission_passes_exact_fences_to_core(phase):
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        return_value=_result(phase=phase, dry_run=False, state='migrating'),
    ) as migrate:
        summary = _command(
            phase=phase,
            apply=True,
            expected_revision=7,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
            canary=True,
            confirm_account_id=22,
        )

    assert summary['phase'] == phase
    assert summary['dry_run'] is False
    migrate.assert_called_once_with(
        tenant_id=11,
        account_id=22,
        phase=phase,
        expected_revision=7,
        expected_source_fingerprint=SOURCE_FINGERPRINT,
        apply=True,
    )


@pytest.mark.parametrize(
    'phase',
    ['reconcile', 'resolve-source', 'confirm-prepare-source'],
)
@override_settings(MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=False)
def test_recovery_apply_remains_available_after_admission_gate_closes(phase):
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        return_value=_result(phase=phase, dry_run=False),
    ) as migrate:
        summary = _command(
            phase=phase,
            apply=True,
            expected_revision=7,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
            canary=True,
            confirm_account_id=22,
        )

    assert summary['dry_run'] is False
    migrate.assert_called_once()


@pytest.mark.parametrize(
    ('options', 'error_code'),
    [
        ({'phase': 'inspect', 'apply': True}, 'apply_not_supported'),
        (
            {
                'phase': 'prepare',
                'apply': True,
                'expected_source_fingerprint': SOURCE_FINGERPRINT,
                'canary': True,
                'confirm_account_id': 22,
            },
            'expected_revision_required',
        ),
        (
            {
                'phase': 'prepare',
                'apply': True,
                'expected_revision': 7,
                'canary': True,
                'confirm_account_id': 22,
            },
            'expected_source_fingerprint_required',
        ),
        (
            {
                'phase': 'prepare',
                'apply': True,
                'expected_revision': 7,
                'expected_source_fingerprint': SOURCE_FINGERPRINT,
                'confirm_account_id': 22,
            },
            'canary_confirmation_required',
        ),
        (
            {
                'phase': 'prepare',
                'apply': True,
                'expected_revision': 7,
                'expected_source_fingerprint': SOURCE_FINGERPRINT,
                'canary': True,
                'confirm_account_id': 21,
            },
            'account_confirmation_mismatch',
        ),
        ({'phase': 'inspect', 'canary': True}, 'dry_run_confirmation_refused'),
        (
            {'phase': 'inspect', 'confirm_account_id': 22},
            'dry_run_confirmation_refused',
        ),
    ],
)
@override_settings(MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=True)
def test_command_rejects_missing_or_ambiguous_apply_confirmation(options, error_code):
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
    ) as migrate, pytest.raises(CommandError) as error:
        _command(**options)

    detail = json.loads(str(error.value))
    assert detail['ok'] is False
    assert detail['error_code'] == error_code
    migrate.assert_not_called()


def test_unexpected_provider_error_is_json_and_does_not_echo_sensitive_details():
    sensitive = (
        'https://feeds.example.test/feed.xml?id=public&key=capability '
        'operator@example.test {"feeds_data": [{"feed_url": "secret"}]}'
    )
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        side_effect=RuntimeError(sensitive),
    ), pytest.raises(CommandError) as error:
        _command()

    rendered = str(error.value)
    detail = json.loads(rendered)
    assert detail == {
        'ok': False,
        'error_code': 'migration_failed',
        'message': 'Feed-profile migration failed for the scoped account.',
    }
    for forbidden in ('https://', '?id=', 'key=', '@example.test', 'feeds_data'):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ('error_name', 'expected_code'),
    [
        ('FeedProfileMigrationConflict', 'state_conflict'),
        ('FeedProfileMigrationSafetyError', 'safety_refused'),
        ('FeedProfileMigrationProviderUncertain', 'provider_outcome_uncertain'),
        ('FeedProfileMigrationError', 'transport_failed'),
    ],
)
def test_core_errors_keep_bounded_actionable_codes_without_provider_details(
    error_name,
    expected_code,
):
    from apps.marketplaces import feed_profile_migration

    error_type = getattr(feed_profile_migration, error_name)
    sensitive = (
        'https://feeds.example.test/feed.xml?id=public&key=capability '
        'operator@example.test'
    )
    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        side_effect=error_type(sensitive),
    ), pytest.raises(CommandError) as error:
        _command()

    rendered = str(error.value)
    detail = json.loads(rendered)
    assert detail == {
        'ok': False,
        'error_code': expected_code,
        'message': 'Feed-profile migration was refused for the scoped account.',
    }
    assert 'https://' not in rendered
    assert 'key=' not in rendered
    assert '@example.test' not in rendered


def test_malformed_core_fields_are_not_serialized():
    result = _result()
    result.state = 'operator@example.test'
    result.target_fingerprint = 'https://secret.test/?key=token'
    result.verification_outcome = {
        'url': 'https://secret.test/?key=token',
    }
    result.foreign_feed_count = ['operator@example.test']

    with patch(
        'apps.marketplaces.feed_profile_migration.run_feed_profile_migration',
        return_value=result,
    ):
        summary = _command()

    assert 'state' not in summary
    assert 'target_fingerprint' not in summary
    assert 'verification_outcome' not in summary
    assert 'foreign_feed_count' not in summary
    assert 'secret' not in json.dumps(summary)
