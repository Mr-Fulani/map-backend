"""Account-scoped operator command for the P6 private artifact canary."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError


def _positive_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError('must be a positive integer') from exc
    if value < 1:
        raise ValueError('must be a positive integer')
    return value


def _error(code: str, message: str) -> CommandError:
    return CommandError(json.dumps(
        {'ok': False, 'error_code': code, 'message': message},
        ensure_ascii=False,
        sort_keys=True,
    ))


class Command(BaseCommand):
    help = (
        'Inspect, activate, or roll back one exact P6 private feed artifact. '
        'Inspect is read-only; mutations require repeated account confirmation.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--account-id', required=True, type=_positive_int)
        parser.add_argument(
            '--phase',
            required=True,
            choices=('inspect', 'activate', 'rollback'),
        )
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--canary', action='store_true')
        parser.add_argument('--confirm-account-id', type=_positive_int)
        parser.add_argument('--expected-artifact-id')
        parser.add_argument('--expected-artifact-revision', type=_positive_int)

    def handle(self, *args, **options):
        phase = options['phase']
        apply = bool(options['apply'])
        account_id = options['account_id']
        if phase == 'inspect' and apply:
            raise _error('apply_not_supported', 'Inspect is read-only.')
        if phase != 'inspect' and not apply:
            raise _error(
                'apply_required',
                'Activate and rollback require --apply.',
            )
        if phase == 'inspect' and (
            options['canary']
            or options['confirm_account_id'] is not None
            or options['expected_artifact_id'] is not None
            or options['expected_artifact_revision'] is not None
        ):
            raise _error(
                'dry_run_confirmation_refused',
                'Mutation confirmations are not accepted for inspect.',
            )
        if apply:
            if not options['canary']:
                raise _error('canary_required', '--canary is required.')
            if options['confirm_account_id'] != account_id:
                raise _error(
                    'account_confirmation_mismatch',
                    '--confirm-account-id must exactly match --account-id.',
                )
        if phase != 'rollback' and (
            options['expected_artifact_id'] is not None
            or options['expected_artifact_revision'] is not None
        ):
            raise _error(
                'rollback_fence_not_allowed',
                'Artifact rollback fences are valid only for rollback.',
            )
        if phase == 'rollback' and (
            options['expected_artifact_id'] is None
            or options['expected_artifact_revision'] is None
        ):
            raise _error(
                'rollback_fence_required',
                'Rollback requires exact artifact id and artifact revision.',
            )

        from apps.marketplaces.feed_artifact_canary import (
            PrivateFeedCanaryError,
            activate_private_feed_canary,
            inspect_private_feed_canary,
            rollback_private_feed_canary,
        )

        try:
            result: Any
            if phase == 'inspect':
                result = inspect_private_feed_canary(account_id)
            elif phase == 'activate':
                result = activate_private_feed_canary(account_id)
            else:
                result = rollback_private_feed_canary(
                    account_id,
                    expected_artifact_id=options['expected_artifact_id'],
                    expected_artifact_revision=(
                        options['expected_artifact_revision']
                    ),
                )
        except (PrivateFeedCanaryError, ValueError):
            raise _error(
                'canary_refused',
                'The exact account-scoped private feed canary was refused.',
            ) from None
        except Exception:
            # Storage/provider exceptions may contain signed URLs, object keys,
            # or response bodies.  Never serialize them into operator output.
            raise _error(
                'canary_failed',
                'The private feed canary failed; inspect protected logs.',
            ) from None

        payload = {
            'ok': True,
            'phase': phase,
            'account_id': result.account_id,
            'endpoint_id': str(result.endpoint_id),
            'source_intent_revision': getattr(
                result,
                'source_intent_revision',
                None,
            ),
            'artifact_revision': result.artifact_revision,
            'listing_count': getattr(result, 'listing_count', None),
            'endpoint_storage_mode': getattr(
                result,
                'endpoint_storage_mode',
                getattr(result, 'storage_mode', None),
            ),
            'profile_state': getattr(result, 'profile_state', None),
            'serve_enabled': getattr(result, 'serve_enabled', None),
            'runtime_ready': getattr(result, 'runtime_ready', None),
            'run_id': str(result.run_id) if hasattr(result, 'run_id') else None,
            'artifact_id': (
                str(result.artifact_id) if hasattr(result, 'artifact_id') else None
            ),
            'size_bytes': getattr(result, 'size_bytes', None),
            'payload_sha256': getattr(result, 'payload_sha256', None),
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
