"""Activate one explicitly allowlisted private feed owner without global cutover."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


def _positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 1 or str(value) != raw_value:
        raise ValueError('must be a canonical positive integer')
    return value


class Command(BaseCommand):
    help = 'Arm one exact account-scoped durable/private feed cutover.'

    def add_arguments(self, parser):
        parser.add_argument('--account-id', required=True, type=_positive_int)
        parser.add_argument('--confirm-account-id', type=_positive_int)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        account_id = options['account_id']
        if not options['apply'] or options['confirm_account_id'] != account_id:
            raise CommandError(
                'Cutover requires --apply and an exact --confirm-account-id.',
            )

        from apps.marketplaces.feed_artifact_clients import (
            private_feed_bucket_preflight,
        )
        from apps.marketplaces.feed_cutover import private_feed_cutover_enabled
        from apps.marketplaces.feed_intents import bump_feed_intents
        from apps.marketplaces.feed_workflow import account_identity_digest
        from apps.marketplaces.models import (
            MarketplaceAccount,
            MarketplaceFeedEndpoint,
            MarketplaceFeedRun,
        )
        from apps.marketplaces.tasks import request_feed_flush

        if not private_feed_cutover_enabled(account_id):
            raise CommandError('The account is not admitted by the active cutover.')
        private_feed_bucket_preflight()

        should_schedule = False
        with transaction.atomic():
            account = (
                MarketplaceAccount.all_objects.select_for_update(of=('self',))
                .select_related('tenant')
                .filter(pk=account_id)
                .first()
            )
            endpoint = (
                MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
                .filter(account_id=account_id)
                .first()
            )
            if account is None or endpoint is None:
                raise CommandError('The account or stable endpoint does not exist.')
            if (
                account.deleted_at is not None
                or account.is_active is not True
                or account.tenant.is_active is not True
                or account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO
                or endpoint.owner_identity_digest
                != account_identity_digest(account)
                or endpoint.profile_state
                != MarketplaceFeedEndpoint.ProfileState.VERIFIED
                or endpoint.serve_enabled is not True
                or endpoint.source_intent_revision
                != account.feed_intent_revision
            ):
                raise CommandError('The account endpoint is not exactly ready.')
            if (
                endpoint.storage_mode
                == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
                and endpoint.current_artifact_id is not None
            ):
                status = 'already_active'
                revision = int(account.feed_intent_revision)
            elif endpoint.storage_mode != (
                MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
            ):
                raise CommandError('The endpoint storage mode is unsupported.')
            elif MarketplaceFeedRun.objects.filter(
                account_id=account.pk,
                state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
            ).exists():
                raise CommandError('An uncertain feed run blocks cutover.')
            elif (
                account.feed_intent_revision
                > account.feed_intent_dispatched_revision
            ):
                if account.feed_intent_due_at is None:
                    raise CommandError('An undispatched feed intent is on hold.')
                status = 'pending_reused'
                revision = int(account.feed_intent_revision)
                should_schedule = True
            else:
                revision = bump_feed_intents(
                    [account.pk],
                    timezone.now(),
                )[account.pk]
                status = 'armed'
                should_schedule = True

        if should_schedule:
            request_feed_flush(account)
        self.stdout.write(json.dumps({
            'ok': True,
            'account_id': account_id,
            'status': status,
            'source_intent_revision': revision,
        }, sort_keys=True))
