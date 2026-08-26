"""Audited, account-scoped reconciliation of one unknown private-feed PUT."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import NoReturn

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime


_REFERENCE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_REVISION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')


def _positive_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError('must be a positive integer') from exc
    if value < 1:
        raise ValueError('must be a positive integer')
    return value


def _aware_datetime(raw_value: str):
    value = parse_datetime(raw_value)
    if value is None or timezone.is_naive(value):
        raise ValueError('must be an ISO-8601 timezone-aware datetime')
    return value


def _reference(raw_value: str) -> str:
    if not isinstance(raw_value, str) or not _REFERENCE_RE.fullmatch(raw_value):
        raise ValueError('must be a bounded operator reference token')
    return raw_value


def _revision_token(raw_value: str) -> str:
    if not isinstance(raw_value, str) or not _REVISION_RE.fullmatch(raw_value):
        raise ValueError('must be a bounded policy revision token')
    return raw_value


def _error(code: str, message: str) -> CommandError:
    return CommandError(json.dumps(
        {'ok': False, 'error_code': code, 'message': message},
        ensure_ascii=False,
        sort_keys=True,
    ))


def _refuse(code: str, message: str) -> NoReturn:
    raise _error(code, message)


def _audit_digest(*, domain: str, reference: str) -> str:
    root_key = str(settings.SECRET_KEY).encode('utf-8')
    return hmac.new(
        root_key,
        b'saas-poster:p6-put-reconciliation:'
        + domain.encode('ascii')
        + b':v1\x00'
        + reference.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


class Command(BaseCommand):
    help = (
        'Reconcile one exact private-feed PUT_PENDING attempt after its origin '
        'process boundary has terminated and the settlement window elapsed.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--tenant-id', required=True, type=_positive_int)
        parser.add_argument('--account-id', required=True, type=_positive_int)
        parser.add_argument('--endpoint-id', required=True)
        parser.add_argument('--run-id', required=True)
        parser.add_argument('--attempt-id', required=True)
        parser.add_argument(
            '--expected-attempt-revision',
            required=True,
            type=_positive_int,
        )
        parser.add_argument(
            '--origin-process-id',
            required=True,
            type=_positive_int,
        )
        parser.add_argument(
            '--origin-process-terminated-at',
            required=True,
            type=_aware_datetime,
        )
        parser.add_argument(
            '--termination-evidence-reference',
            required=True,
            type=_reference,
        )
        parser.add_argument(
            '--operator-reference',
            required=True,
            type=_reference,
        )
        parser.add_argument(
            '--origin-process-reference',
            required=True,
            type=_reference,
        )
        parser.add_argument(
            '--identity-digest-key-revision',
            required=True,
            type=_revision_token,
        )
        parser.add_argument(
            '--canary-policy-revision',
            required=True,
            type=_revision_token,
        )
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--canary', action='store_true')
        parser.add_argument('--confirm-account-id', type=_positive_int)
        parser.add_argument(
            '--confirm-origin-process-terminated',
            action='store_true',
        )

    def handle(self, *args, **options):
        account_id = options['account_id']
        if not options['apply']:
            _refuse('apply_required', 'Reconciliation requires --apply.')
        if not options['canary']:
            _refuse('canary_required', 'Reconciliation requires --canary.')
        if options['confirm_account_id'] != account_id:
            _refuse(
                'account_confirmation_mismatch',
                '--confirm-account-id must exactly match --account-id.',
            )
        if not options['confirm_origin_process_terminated']:
            _refuse(
                'origin_termination_confirmation_required',
                'Origin process termination must be explicitly confirmed.',
            )

        from apps.marketplaces.feed_artifact_clients import (
            private_feed_authoritative_version_client,
            private_feed_bucket_preflight,
        )
        from apps.marketplaces.feed_artifact_put_reconciliation import (
            PutOriginTerminationAttestation,
            PutPendingAttemptReference,
            PutPendingReconciliationError,
            reconcile_put_pending_upload_attempt,
        )

        evidence_reference = options['termination_evidence_reference']
        operator_reference = options['operator_reference']
        origin_reference = options['origin_process_reference']
        termination = PutOriginTerminationAttestation(
            evidence_reference=evidence_reference,
            evidence_digest=_audit_digest(
                domain='evidence',
                reference=evidence_reference,
            ),
            operator_identity_digest=_audit_digest(
                domain='operator',
                reference=operator_reference,
            ),
            origin_process_identity_digest=_audit_digest(
                domain='origin-process',
                reference=origin_reference,
            ),
            digest_scheme_revision='hmac-sha256-v1',
            identity_digest_key_revision=(
                options['identity_digest_key_revision']
            ),
            origin_process_id=options['origin_process_id'],
            origin_process_terminated_at=(
                options['origin_process_terminated_at']
            ),
            operator_confirmed=True,
        )
        reference = PutPendingAttemptReference(
            tenant_id=options['tenant_id'],
            account_id=account_id,
            endpoint_id=options['endpoint_id'],
            run_id=options['run_id'],
            attempt_id=options['attempt_id'],
            expected_revision=options['expected_attempt_revision'],
        )
        try:
            private_feed_bucket_preflight()
            client = private_feed_authoritative_version_client(
                canary_policy_revision=options['canary_policy_revision'],
            )
            result = reconcile_put_pending_upload_attempt(
                reference,
                client=client,
                termination=termination,
            )
        except PutPendingReconciliationError as exc:
            raise _error(
                exc.code,
                'The exact PUT-pending reconciliation was refused.',
            ) from None
        except Exception:
            # SDK errors may carry the bucket, key, request ID, or credentials.
            raise _error(
                'reconciliation_failed',
                'The exact PUT-pending reconciliation failed; inspect protected logs.',
            ) from None

        from apps.marketplaces.feed_cutover import private_feed_cutover_enabled

        if (
            result.applied
            and result.state in {'no_object', 'version_known'}
            and private_feed_cutover_enabled(account_id)
        ):
            from django.db import transaction

            from apps.marketplaces.feed_intents import (
                nudge_undispatched_feed_intent,
            )
            from apps.marketplaces.models import MarketplaceAccount

            try:
                with transaction.atomic():
                    nudge_undispatched_feed_intent(
                        account_id,
                        timezone.now(),
                    )
            except MarketplaceAccount.DoesNotExist:
                # Reconciliation evidence is already durable. A concurrently
                # removed owner has no safe work left to wake.
                pass

        payload = {
            'ok': True,
            'phase': 'reconcile_put',
            'account_id': account_id,
            'attempt_id': str(result.attempt_id),
            'outcome': result.outcome,
            'state': result.state,
            'revision': result.revision,
            'applied': result.applied,
            'pages_scanned': result.pages_scanned,
            'entries_scanned': result.entries_scanned,
            'exact_version_count': result.exact_version_count,
            'exact_delete_marker_count': result.exact_delete_marker_count,
            'settlement_remaining_seconds': (
                result.settlement_remaining_seconds
            ),
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
