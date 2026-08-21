"""Bounded, resumable backfill for the marketplace status lifecycle cursors."""

import datetime
import hashlib
import json
import time
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, models, transaction
from django.db.models import Min
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware

from apps.marketplaces.models import Listing, MarketplaceAccount


DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 1_000
ACTIVE_JITTER_SECONDS = 24 * 60 * 60
TRANSIENT_JITTER_SECONDS = 10 * 60
ELIGIBLE_STATUSES = (
    Listing.STATUS_PENDING,
    Listing.STATUS_ACTIVE,
    Listing.STATUS_ARCHIVING,
)


def _due_at(*, anchor: datetime.datetime, listing_id: int, status: str) -> datetime.datetime:
    """Return a stable due time inside the status-specific window."""
    window = (
        ACTIVE_JITTER_SECONDS
        if status == Listing.STATUS_ACTIVE
        else TRANSIENT_JITTER_SECONDS
    )
    digest = hashlib.blake2s(
        f'{listing_id}:{status}'.encode('ascii'),
        digest_size=8,
    ).digest()
    offset = int.from_bytes(digest, byteorder='big') % window
    return anchor + datetime.timedelta(seconds=offset)


class Command(BaseCommand):
    """Backfill listing due cursors without provider calls or task dispatch."""

    help = (
        'Backfill nullable listing/account status lifecycle due cursors in '
        'bounded, resumable batches.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--anchor',
            required=True,
            help='Aware UTC ISO-8601 anchor, for example 2026-08-13T00:00:00Z.',
        )
        parser.add_argument('--tenant-id', type=int)
        parser.add_argument('--account-id', type=int)
        parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument('--max-rows', type=int)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        started_at = time.monotonic()
        dry_run = bool(options['dry_run'])
        self._validate_mode(dry_run=dry_run)
        anchor = self._parse_anchor(options['anchor'])
        batch_size = self._positive_int(
            '--batch-size',
            options['batch_size'],
            maximum=MAX_BATCH_SIZE,
        )
        max_rows = options['max_rows']
        if max_rows is not None:
            max_rows = self._positive_int('--max-rows', max_rows)
        tenant_id = self._optional_positive_int('--tenant-id', options['tenant_id'])
        account_id = self._optional_positive_int('--account-id', options['account_id'])
        if connection.vendor != 'postgresql':
            raise CommandError('Backfill поддерживает только PostgreSQL.')

        eligible_before = self._listings(
            tenant_id=tenant_id,
            account_id=account_id,
            due_is_null=True,
        ).count()
        claim_mismatches_before = self._claim_mismatch_counts(
            tenant_id=tenant_id,
            account_id=account_id,
        )
        summary = {
            'mode': 'dry_run' if dry_run else 'apply',
            'anchor': anchor.isoformat().replace('+00:00', 'Z'),
            'batch_size': batch_size,
            'max_rows': max_rows,
            'eligible_before': eligible_before,
            'candidates': 0,
            'considered': 0,
            'would_update': 0,
            'updated': 0,
            'skipped_concurrent': 0,
            'batches': 0,
            'accounts_would_update': 0,
            'accounts_updated': 0,
            'status_counts': {status: 0 for status in ELIGIBLE_STATUSES},
            'eligible_after': eligible_before,
            'last_pk': None,
            'duration_seconds': 0.0,
            'claim_mismatches_before': claim_mismatches_before,
            'claim_mismatches_after': claim_mismatches_before,
        }

        last_pk = 0
        attempted = 0
        account_ids_would_update: set[int] = set()
        account_ids_updated: set[int] = set()
        while max_rows is None or attempted < max_rows:
            remaining = batch_size
            if max_rows is not None:
                remaining = min(remaining, max_rows - attempted)
            candidates = list(
                self._listings(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    due_is_null=True,
                )
                .filter(pk__gt=last_pk)
                .order_by('pk')
                .values('pk', 'account_id', 'status')[:remaining]
            )
            if not candidates:
                break

            last_pk = candidates[-1]['pk']
            summary['last_pk'] = last_pk
            summary['batches'] += 1
            summary['candidates'] += len(candidates)
            # ``--max-rows`` is an operational bound on rows attempted, not
            # merely rows whose locks happened to be available.  Otherwise a
            # canary could walk an entire large account while repeatedly
            # skipping locked rows.  A later invocation safely revisits them
            # through the persistent ``next_status_check_at IS NULL`` cursor.
            attempted += len(candidates)

            if dry_run:
                account_changes = self._dry_run_account_changes(
                    candidates=candidates,
                    anchor=anchor,
                    tenant_id=tenant_id,
                    account_id=account_id,
                )
                account_ids_would_update.update(account_changes)
                summary['considered'] += len(candidates)
                summary['would_update'] += len(candidates)
                status_counts = Counter(row['status'] for row in candidates)
                for status, count in status_counts.items():
                    summary['status_counts'][status] += count
                continue

            result = self._apply_batch(
                candidates=candidates,
                anchor=anchor,
                tenant_id=tenant_id,
                account_id=account_id,
            )
            summary['considered'] += result['considered']
            summary['updated'] += result['updated']
            account_ids_updated.update(result['account_ids_updated'])
            summary['skipped_concurrent'] += len(candidates) - result['considered']
            for status, count in result['status_counts'].items():
                summary['status_counts'][status] += count

        summary['accounts_would_update'] = len(account_ids_would_update)
        summary['accounts_updated'] = len(account_ids_updated)
        repair_summary = self._repair_account_cursors(
            tenant_id=tenant_id,
            account_id=account_id,
            dry_run=dry_run,
        )
        summary.update(repair_summary)
        summary['eligible_after'] = self._listings(
            tenant_id=tenant_id,
            account_id=account_id,
            due_is_null=True,
        ).count()
        summary['duration_seconds'] = round(time.monotonic() - started_at, 6)
        summary['claim_mismatches_after'] = self._claim_mismatch_counts(
            tenant_id=tenant_id,
            account_id=account_id,
        )
        self.stdout.write(json.dumps(summary, sort_keys=True, separators=(',', ':')))

    def _repair_account_cursors(
        self,
        *,
        tenant_id: int | None,
        account_id: int | None,
        dry_run: bool,
    ) -> dict[str, int]:
        """Reconcile denormalized account cursors from all already-due rows.

        This is deliberately independent of the NULL-listing backfill. It
        repairs partial prior runs, activation cycles, and rollback/re-enable
        windows where listings already have cursors but their account does not.
        """

        initial_due_minima = self._all_due_minima(
            tenant_id=tenant_id,
            account_id=account_id,
        )
        accounts = MarketplaceAccount.all_objects.filter(
            deleted_at__isnull=True,
            is_active=True,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        )
        if tenant_id is not None:
            accounts = accounts.filter(tenant_id=tenant_id)
        if account_id is not None:
            accounts = accounts.filter(pk=account_id)
        mismatched_ids = [
            account.pk
            for account in accounts.only('pk', 'status_batch_due_at').order_by('pk')
            if account.status_batch_due_at != initial_due_minima.get(account.pk)
        ]
        if not dry_run:
            for offset in range(0, len(mismatched_ids), MAX_BATCH_SIZE):
                batch_ids = mismatched_ids[offset:offset + MAX_BATCH_SIZE]
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout TO '1s'")
                        cursor.execute("SET LOCAL statement_timeout TO '15s'")
                    locked = list(
                        MarketplaceAccount.all_objects.select_for_update(
                            skip_locked=True,
                        )
                        .filter(pk__in=batch_ids, deleted_at__isnull=True, is_active=True)
                        .order_by('pk')
                        .only('pk', 'status_batch_due_at')
                    )
                    # Recompute after owning every account row in this batch.
                    # A concurrent lifecycle writer must take the same account
                    # lock before nudging its listing/account due cursor, so it
                    # can no longer be overwritten by a stale pre-lock MIN.
                    due_minima = self._due_minima(
                        account_ids={current.pk for current in locked},
                        tenant_id=tenant_id,
                        account_id=account_id,
                    )
                    changed = []
                    for current in locked:
                        expected_due = due_minima.get(current.pk)
                        if current.status_batch_due_at != expected_due:
                            current.status_batch_due_at = expected_due
                            changed.append(current)
                    if changed:
                        MarketplaceAccount.all_objects.bulk_update(
                            changed,
                            ['status_batch_due_at'],
                            batch_size=MAX_BATCH_SIZE,
                        )
        remaining = 0 if dry_run else self._count_account_cursor_mismatches(
            tenant_id=tenant_id,
            account_id=account_id,
        )
        return {
            'account_cursor_mismatches_before': len(mismatched_ids),
            'account_cursors_would_repair': len(mismatched_ids) if dry_run else 0,
            'account_cursors_repaired': (
                0 if dry_run else max(0, len(mismatched_ids) - remaining)
            ),
            'account_cursor_mismatches_after': (
                len(mismatched_ids) if dry_run else remaining
            ),
        }

    def _all_due_minima(
        self,
        *,
        tenant_id: int | None,
        account_id: int | None,
    ) -> dict[int, datetime.datetime]:
        rows = (
            self._listings(
                tenant_id=tenant_id,
                account_id=account_id,
                due_is_null=False,
            )
            .values('account_id')
            .annotate(min_due=Min('next_status_check_at'))
        )
        return {row['account_id']: row['min_due'] for row in rows}

    def _count_account_cursor_mismatches(
        self,
        *,
        tenant_id: int | None,
        account_id: int | None,
    ) -> int:
        due_minima = self._all_due_minima(
            tenant_id=tenant_id,
            account_id=account_id,
        )
        accounts = MarketplaceAccount.all_objects.filter(
            deleted_at__isnull=True,
            is_active=True,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        )
        if tenant_id is not None:
            accounts = accounts.filter(tenant_id=tenant_id)
        if account_id is not None:
            accounts = accounts.filter(pk=account_id)
        return sum(
            account.status_batch_due_at != due_minima.get(account.pk)
            for account in accounts.only('pk', 'status_batch_due_at').iterator()
        )

    @staticmethod
    def _validate_mode(*, dry_run: bool) -> None:
        mode = str(
            getattr(settings, 'AVITO_STATUS_LIFECYCLE_MODE', ''),
        ).strip().lower()
        if not dry_run and mode != 'dual_write':
            raise CommandError(
                'Backfill разрешён только при '
                'AVITO_STATUS_LIFECYCLE_MODE=dual_write.',
            )

    @staticmethod
    def _parse_anchor(value: str) -> datetime.datetime:
        anchor = parse_datetime(str(value))
        if (
            anchor is None
            or not is_aware(anchor)
            or anchor.utcoffset() != datetime.timedelta(0)
        ):
            raise CommandError('--anchor должен быть aware UTC ISO-8601 timestamp.')
        return anchor.astimezone(datetime.timezone.utc)

    @staticmethod
    def _positive_int(name: str, value: int, *, maximum: int | None = None) -> int:
        if value <= 0:
            raise CommandError(f'{name} должен быть положительным целым числом.')
        if maximum is not None and value > maximum:
            raise CommandError(f'{name} не может превышать {maximum}.')
        return value

    @classmethod
    def _optional_positive_int(cls, name: str, value: int | None) -> int | None:
        return None if value is None else cls._positive_int(name, value)

    @staticmethod
    def _listings(
        *,
        tenant_id: int | None,
        account_id: int | None,
        due_is_null: bool,
    ):
        queryset = Listing.all_objects.filter(
            deleted_at__isnull=True,
            tenant__is_active=True,
            tenant_id=models.F('account__tenant_id'),
            account__deleted_at__isnull=True,
            account__is_active=True,
            account__marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            external_id__isnull=False,
            next_status_check_at__isnull=due_is_null,
            status__in=ELIGIBLE_STATUSES,
        ).exclude(external_id='')
        if tenant_id is not None:
            queryset = queryset.filter(
                tenant_id=tenant_id,
                account__tenant_id=tenant_id,
            )
        if account_id is not None:
            queryset = queryset.filter(account_id=account_id)
        return queryset

    @staticmethod
    def _claim_mismatch_counts(
        *,
        tenant_id: int | None,
        account_id: int | None,
    ) -> dict[str, int]:
        listing_scope = Listing.all_objects.filter(
            deleted_at__isnull=True,
            tenant_id=models.F('account__tenant_id'),
        )
        account_scope = MarketplaceAccount.all_objects.all()
        if tenant_id is not None:
            listing_scope = listing_scope.filter(tenant_id=tenant_id)
            account_scope = account_scope.filter(tenant_id=tenant_id)
        if account_id is not None:
            listing_scope = listing_scope.filter(account_id=account_id)
            account_scope = account_scope.filter(pk=account_id)
        listing_mismatch = (
            models.Q(
                status_check_claim_token__isnull=True,
                status_check_claimed_until__isnull=False,
            )
            | models.Q(
                status_check_claim_token__isnull=False,
                status_check_claimed_until__isnull=True,
            )
        )
        account_mismatch = (
            models.Q(
                status_batch_claim_token__isnull=True,
                status_batch_claimed_until__isnull=False,
            )
            | models.Q(
                status_batch_claim_token__isnull=False,
                status_batch_claimed_until__isnull=True,
            )
        )
        return {
            'listing': listing_scope.filter(listing_mismatch).count(),
            'account': account_scope.filter(account_mismatch).count(),
        }

    def _dry_run_account_changes(
        self,
        *,
        candidates: list[dict],
        anchor: datetime.datetime,
        tenant_id: int | None,
        account_id: int | None,
    ) -> set[int]:
        projected_minima: dict[int, datetime.datetime] = {}
        for row in candidates:
            due_at = _due_at(
                anchor=anchor,
                listing_id=row['pk'],
                status=row['status'],
            )
            current = projected_minima.get(row['account_id'])
            projected_minima[row['account_id']] = (
                due_at if current is None else min(current, due_at)
            )

        account_ids = set(projected_minima)
        existing_minima = self._due_minima(
            account_ids=account_ids,
            tenant_id=tenant_id,
            account_id=account_id,
        )
        for current_account_id, due_at in existing_minima.items():
            current = projected_minima.get(current_account_id)
            projected_minima[current_account_id] = (
                due_at if current is None else min(current, due_at)
            )

        accounts = MarketplaceAccount.all_objects.filter(
            pk__in=account_ids,
            deleted_at__isnull=True,
            is_active=True,
        ).only('pk', 'status_batch_due_at')
        return {
            account.pk
            for account in accounts
            if projected_minima.get(account.pk) is not None
            and (
                account.status_batch_due_at is None
                or projected_minima[account.pk] < account.status_batch_due_at
            )
        }

    def _apply_batch(
        self,
        *,
        candidates: list[dict],
        anchor: datetime.datetime,
        tenant_id: int | None,
        account_id: int | None,
    ) -> dict:
        candidate_ids = [row['pk'] for row in candidates]
        candidate_account_ids = {row['account_id'] for row in candidates}

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout TO '1s'")
                cursor.execute("SET LOCAL statement_timeout TO '15s'")

            accounts = list(
                MarketplaceAccount.all_objects.select_for_update(skip_locked=True)
                .filter(
                    pk__in=candidate_account_ids,
                    deleted_at__isnull=True,
                    is_active=True,
                )
                .order_by('pk')
                .only('pk', 'status_batch_due_at')
            )
            locked_account_ids = {account.pk for account in accounts}
            rows = list(
                self._listings(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    due_is_null=True,
                )
                .select_for_update(skip_locked=True, of=('self',))
                .filter(
                    pk__in=candidate_ids,
                    account_id__in=locked_account_ids,
                )
                .order_by('pk')
                .values('pk', 'account_id', 'status')
            )
            if not rows:
                return {
                    'considered': 0,
                    'updated': 0,
                    'account_ids_updated': set(),
                    'status_counts': {},
                }

            due_by_id = {
                row['pk']: _due_at(
                    anchor=anchor,
                    listing_id=row['pk'],
                    status=row['status'],
                )
                for row in rows
            }
            due_case = models.Case(
                *(
                    models.When(pk=listing_id, then=models.Value(due_at))
                    for listing_id, due_at in due_by_id.items()
                ),
                output_field=models.DateTimeField(),
            )
            updated = self._listings(
                tenant_id=tenant_id,
                account_id=account_id,
                due_is_null=True,
            ).filter(pk__in=due_by_id).update(next_status_check_at=due_case)
            if updated != len(rows):
                raise CommandError(
                    'Условный update изменил неожиданное число Listing; '
                    'транзакция отменена, команду безопасно повторить.',
                )

            touched_account_ids = {row['account_id'] for row in rows}
            minima = self._due_minima(
                account_ids=touched_account_ids,
                tenant_id=tenant_id,
                account_id=account_id,
            )
            changed_accounts = []
            for account in accounts:
                due_at = minima.get(account.pk)
                if due_at is None:
                    continue
                if (
                    account.status_batch_due_at is None
                    or due_at < account.status_batch_due_at
                ):
                    account.status_batch_due_at = due_at
                    changed_accounts.append(account)
            if changed_accounts:
                MarketplaceAccount.all_objects.bulk_update(
                    changed_accounts,
                    ['status_batch_due_at'],
                    batch_size=MAX_BATCH_SIZE,
                )

        return {
            'considered': len(rows),
            'updated': updated,
            'account_ids_updated': {account.pk for account in changed_accounts},
            'status_counts': dict(Counter(row['status'] for row in rows)),
        }

    def _due_minima(
        self,
        *,
        account_ids: set[int],
        tenant_id: int | None,
        account_id: int | None,
    ) -> dict[int, datetime.datetime]:
        if not account_ids:
            return {}
        rows = (
            self._listings(
                tenant_id=tenant_id,
                account_id=account_id,
                due_is_null=False,
            )
            .filter(account_id__in=account_ids)
            .values('account_id')
            .annotate(min_due=Min('next_status_check_at'))
        )
        return {row['account_id']: row['min_due'] for row in rows}
