"""Account-scoped, resumable migration of one marketplace feed profile."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


_PHASE_INSPECT = 'inspect'
_MUTATING_PHASES = frozenset({
    'prepare',
    'migrate',
    'reconcile',
    'resolve-source',
    'confirm-prepare-source',
})
_ADMISSION_PHASES = frozenset({'prepare', 'migrate'})
_PHASES = (
    _PHASE_INSPECT,
    'prepare',
    'migrate',
    'reconcile',
    'resolve-source',
    'confirm-prepare-source',
)
_FINGERPRINT_RE = re.compile(r'^[0-9a-f]{64}$')
_ERROR_CODE_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_SAFE_TOKEN_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_MAX_PROFILE_REVISION = (1 << 63) - 1

# Never serialize a provider profile object (or arbitrary service output).  The
# command is often copied into incident tickets, so this list deliberately
# excludes URLs, capability tokens, report email, credentials and feed data.
_SAFE_SUMMARY_FIELDS = (
    'state',
    'revision',
    'source_fingerprint',
    'target_fingerprint',
    'verification_outcome',
    'owned_feed_count',
    'foreign_feed_count',
    'parity_verified',
    'settlement_remaining_seconds',
)


def _positive_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError('must be a positive integer') from exc
    if value <= 0:
        raise ValueError('must be a positive integer')
    return value


def _nonnegative_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError('must be a non-negative integer') from exc
    if not 0 <= value <= _MAX_PROFILE_REVISION:
        raise ValueError('must be a bounded non-negative integer')
    return value


def _fingerprint(raw_value: str) -> str:
    value = str(raw_value or '')
    if not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError('must be an exact lowercase SHA-256 fingerprint')
    return value


def _json_command_error(code: object, message: str) -> CommandError:
    """Build one bounded JSON error without serializing provider details."""

    candidate = str(code or '')
    safe_code = (
        candidate
        if _ERROR_CODE_RE.fullmatch(candidate)
        else 'migration_failed'
    )
    return CommandError(json.dumps(
        {
            'ok': False,
            'error_code': safe_code,
            'message': message,
        },
        ensure_ascii=False,
        sort_keys=True,
    ))


def _summary_value(summary, field: str):
    if isinstance(summary, Mapping):
        return summary.get(field)
    return getattr(summary, field, None)


def _safe_summary_value(field: str, value):
    """Validate the shape as well as the name of every output field."""

    if value is None:
        return True, None
    if field in {'state', 'verification_outcome'}:
        return bool(
            isinstance(value, str) and _SAFE_TOKEN_RE.fullmatch(value),
        ), value
    if field in {'source_fingerprint', 'target_fingerprint'}:
        return bool(
            isinstance(value, str)
            and (value == '' or _FINGERPRINT_RE.fullmatch(value)),
        ), value
    if field in {'revision', 'owned_feed_count', 'foreign_feed_count'}:
        return type(value) is int and value >= 0, value
    if field == 'settlement_remaining_seconds':
        return type(value) is int and 0 <= value <= 86400, value
    if field == 'parity_verified':
        return type(value) is bool, value
    return False, None


def _safe_summary(summary, *, options: dict) -> dict:
    """Return only scalar, operator-safe fields from the core result."""

    result = {
        'ok': True,
        'phase': options['phase'],
        'dry_run': not bool(options['apply']),
        'account_id': options['account_id'],
        'tenant_id': options['tenant_id'],
    }
    for field in _SAFE_SUMMARY_FIELDS:
        value = _summary_value(summary, field)
        safe, normalized = _safe_summary_value(field, value)
        if safe:
            result[field] = normalized
    return result


def _validate_runtime_options(options: dict) -> None:
    """Repeat argparse fences for programmatic ``call_command`` callers."""

    if options.get('phase') not in _PHASES:
        raise _json_command_error(
            'invalid_phase',
            '--phase is invalid.',
        )
    for field in ('tenant_id', 'account_id'):
        value = options.get(field)
        if type(value) is not int or value <= 0:
            raise _json_command_error(
                'invalid_scope',
                '--tenant-id and --account-id must be positive integers.',
            )
    revision = options.get('expected_revision')
    if (
        revision is not None
        and (
            type(revision) is not int
            or not 0 <= revision <= _MAX_PROFILE_REVISION
        )
    ):
        raise _json_command_error(
            'invalid_expected_revision',
            '--expected-revision is invalid.',
        )
    fingerprint = options.get('expected_source_fingerprint')
    if (
        fingerprint is not None
        and (
            not isinstance(fingerprint, str)
            or not _FINGERPRINT_RE.fullmatch(fingerprint)
        )
    ):
        raise _json_command_error(
            'invalid_expected_source_fingerprint',
            '--expected-source-fingerprint is invalid.',
        )
    confirmation = options.get('confirm_account_id')
    if (
        confirmation is not None
        and (type(confirmation) is not int or confirmation <= 0)
    ):
        raise _json_command_error(
            'invalid_account_confirmation',
            '--confirm-account-id is invalid.',
        )
    if type(options.get('apply')) is not bool or type(options.get('canary')) is not bool:
        raise _json_command_error(
            'invalid_confirmation_flags',
            '--apply and --canary must be boolean flags.',
        )


class Command(BaseCommand):
    help = (
        'Inspect or explicitly advance one Avito feed-profile migration. '
        'The default is a non-mutating dry-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--tenant-id', required=True, type=_positive_int)
        parser.add_argument('--account-id', required=True, type=_positive_int)
        parser.add_argument('--phase', required=True, choices=_PHASES)
        parser.add_argument(
            '--expected-revision',
            type=_nonnegative_int,
            help='Exact endpoint profile revision observed in a prior inspect.',
        )
        parser.add_argument(
            '--expected-source-fingerprint',
            type=_fingerprint,
            help='Exact source profile SHA-256 observed in a prior inspect.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist this one reviewed account transition.',
        )
        parser.add_argument(
            '--canary',
            action='store_true',
            help=(
                'Operator acknowledgement for this reviewed account only; '
                'it is not proof of the required real provider query/HTTP 307 '
                'canary.'
            ),
        )
        parser.add_argument(
            '--confirm-account-id',
            type=_positive_int,
            help='Repeat --account-id before any mutation is allowed.',
        )

    def handle(self, *args, **options):
        _validate_runtime_options(options)
        phase = options['phase']
        apply = bool(options['apply'])

        if apply and phase == _PHASE_INSPECT:
            raise _json_command_error(
                'apply_not_supported',
                'The inspect phase is read-only; remove --apply.',
            )
        if not apply and (options['canary'] or options['confirm_account_id'] is not None):
            raise _json_command_error(
                'dry_run_confirmation_refused',
                '--canary and --confirm-account-id are valid only with --apply.',
            )
        if apply:
            if phase not in _MUTATING_PHASES:
                raise _json_command_error(
                    'apply_not_supported',
                    'This phase does not support --apply.',
                )
            if options['expected_revision'] is None:
                raise _json_command_error(
                    'expected_revision_required',
                    '--expected-revision is required with --apply.',
                )
            if options['expected_source_fingerprint'] is None:
                raise _json_command_error(
                    'expected_source_fingerprint_required',
                    '--expected-source-fingerprint is required with --apply.',
                )
            if not options['canary']:
                raise _json_command_error(
                    'canary_confirmation_required',
                    '--canary is required with --apply.',
                )
            if options['confirm_account_id'] != options['account_id']:
                raise _json_command_error(
                    'account_confirmation_mismatch',
                    '--confirm-account-id must exactly match --account-id.',
                )
            if (
                phase in _ADMISSION_PHASES
                and not getattr(
                    settings,
                    'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED',
                    False,
                )
            ):
                raise _json_command_error(
                    'migration_admission_disabled',
                    'New feed-profile migration admission is disabled.',
                )

        # Import lazily so command-level validation cannot accidentally create
        # an adapter, start provider I/O or expose configuration details.
        from apps.marketplaces.feed_profile_migration import (
            FeedProfileMigrationError,
            run_feed_profile_migration,
        )

        try:
            summary = run_feed_profile_migration(
                tenant_id=options['tenant_id'],
                account_id=options['account_id'],
                phase=phase,
                expected_revision=options['expected_revision'],
                expected_source_fingerprint=(
                    options['expected_source_fingerprint']
                ),
                apply=apply,
            )
        except FeedProfileMigrationError as exc:
            code = getattr(exc, 'code', 'migration_refused')
            raise _json_command_error(
                code,
                'Feed-profile migration was refused for the scoped account.',
            ) from None
        except Exception:
            # Provider/client exceptions can contain full request URLs and
            # response bodies.  Never echo them from this operator command.
            raise _json_command_error(
                'migration_failed',
                'Feed-profile migration failed for the scoped account.',
            ) from None

        self.stdout.write(json.dumps(
            _safe_summary(summary, options=options),
            ensure_ascii=False,
            sort_keys=True,
        ))
