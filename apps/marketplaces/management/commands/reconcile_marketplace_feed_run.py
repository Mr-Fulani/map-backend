"""Explicit operator reconciliation for an ambiguous marketplace feed POST."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.marketplaces.feed_workflow import (
    FEED_RUN_RECONCILIATION_RESOLUTIONS,
    RECONCILIATION_PROVIDER_ACCEPTED,
    RECONCILIATION_PROVIDER_NOT_ACCEPTED,
    FeedWorkflowError,
    reconcile_uncertain_feed_run,
)
from apps.marketplaces.models import MarketplaceAccount


def _durable_feed_runtime_enabled() -> bool:
    return (
        settings.MARKETPLACE_FEED_RUN_MODE == 'durable'
        and settings.AVITO_STATUS_LIFECYCLE_MODE == 'dual_write'
    )


def _json_command_error(code: str, error: object) -> CommandError:
    return CommandError(json.dumps(
        {
            'ok': False,
            'error_code': code,
            'message': str(error),
        },
        ensure_ascii=False,
        sort_keys=True,
    ))


class Command(BaseCommand):
    help = (
        'Dry-run or explicitly resolve one MarketplaceFeedRun whose provider '
        'submission outcome is uncertain.'
    )

    def add_arguments(self, parser):
        parser.add_argument('run_id')
        parser.add_argument('--expected-revision', required=True, type=int)
        parser.add_argument(
            '--resolution',
            required=True,
            choices=FEED_RUN_RECONCILIATION_RESOLUTIONS,
        )
        parser.add_argument('--provider-run-id')
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist the reviewed resolution. The default is rollback-only dry-run.',
        )

    def handle(self, *args, **options):
        apply_resolution = bool(options['apply'])
        resolution = options['resolution']
        runtime_enabled = _durable_feed_runtime_enabled()
        if (
            apply_resolution
            and resolution == RECONCILIATION_PROVIDER_ACCEPTED
            and not runtime_enabled
        ):
            raise _json_command_error(
                'runtime_mode_disabled',
                'provider_accepted requires MARKETPLACE_FEED_RUN_MODE=durable '
                'and AVITO_STATUS_LIFECYCLE_MODE=dual_write.',
            )

        dispatch_id = None
        flush_scheduled = False
        try:
            with transaction.atomic():
                snapshot = reconcile_uncertain_feed_run(
                    options['run_id'],
                    expected_revision=options['expected_revision'],
                    resolution=resolution,
                    provider_run_id=options['provider_run_id'],
                    allow_tombstone=(
                        resolution == RECONCILIATION_PROVIDER_NOT_ACCEPTED
                    ),
                )
                owner_is_live = MarketplaceAccount.objects.filter(
                    pk=snapshot.account_id,
                    is_active=True,
                    tenant__is_active=True,
                ).exists()
                if not apply_resolution:
                    # Exercise the same account/run locks, identity fence and
                    # database constraints as --apply, then discard the write.
                    transaction.set_rollback(True)
                elif resolution == RECONCILIATION_PROVIDER_ACCEPTED:
                    from apps.marketplaces.tasks import _enqueue_feed_run_snapshot

                    dispatch = _enqueue_feed_run_snapshot(snapshot)
                    if dispatch is None:
                        raise FeedWorkflowError(
                            'Accepted reconciliation did not produce a due feed step.',
                        )
                    dispatch_id = str(dispatch.pk)
                elif runtime_enabled:
                    from apps.marketplaces.tasks import (
                        _enqueue_feed_run_snapshot,
                        coalesced_flush_task,
                    )

                    dispatch = _enqueue_feed_run_snapshot(snapshot)
                    if dispatch is None:
                        raise FeedWorkflowError(
                            'Rejected reconciliation did not produce a terminal '
                            'feed digest.',
                        )
                    dispatch_id = str(dispatch.pk)

                    if owner_is_live:
                        account_id = snapshot.account_id
                        transaction.on_commit(
                            lambda: coalesced_flush_task.delay(account_id),
                        )
                        flush_scheduled = True
        except (FeedWorkflowError, ValueError) as exc:
            raise _json_command_error('reconciliation_refused', exc) from exc

        result = {
            'ok': True,
            'mode': 'apply' if apply_resolution else 'dry_run',
            'run_id': str(snapshot.run_id),
            'account_id': snapshot.account_id,
            'marketplace': snapshot.marketplace,
            'resolution': resolution,
            'state': snapshot.state,
            'revision': snapshot.revision,
            'provider_run_id': snapshot.provider_run_id,
            'owner_restored': False,
            'next_attempt_at': (
                snapshot.next_attempt_at.isoformat()
                if snapshot.next_attempt_at is not None
                else None
            ),
            'finished_at': (
                snapshot.finished_at.isoformat()
                if snapshot.finished_at is not None
                else None
            ),
            'dispatch_id': dispatch_id,
            'flush_scheduled': flush_scheduled,
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
