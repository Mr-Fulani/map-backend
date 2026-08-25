"""Bounded, page-driven reconciliation of Avito Autoload item errors.

The provider endpoint cannot filter by our ``ad_id``.  This workflow therefore
scans each remote report page once and maps at most 100 returned identifiers to
local listings, instead of scanning all report pages once per local batch.
"""

from dataclasses import dataclass
import hashlib
import html
import logging
import re
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from django.utils import timezone
import requests

from apps.core.advisory_lock import try_session_advisory_lock
from apps.marketplaces.adapters.avito.adapter import (
    AvitoAdapter,
    FeedItemErrorPage,
)
from apps.marketplaces.adapters.avito.error_handler import AvitoError
from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError
from apps.marketplaces.feed_intents import bump_feed_intents
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.notifications.services import LEVEL_ERROR
from apps.notifications.tasks import send_notification_task
from apps.sync.models import SyncLog


logger = logging.getLogger(__name__)
coordination_cache = caches['coordination']

FEED_REPORT_PAGE_SIZE = 100
FEED_REPORT_REASON_MAX_LENGTH = 2000
FEED_REPORT_PAGE_DELAY_SECONDS = 15
FEED_REPORT_MAX_RETRY_DELAY_SECONDS = 300
FEED_REPORT_MAX_PAGE_ATTEMPTS = 5
FEED_REPORT_SCHEDULE_DEDUP_SECONDS = 6 * 60 * 60
_FLUSH_MARKER_MAX_LENGTH = 64


@dataclass(frozen=True)
class _ApplyResult:
    account_available: bool
    tenant_id: int | None = None
    changed_count: int = 0
    skipped_locked: int = 0
    stale_reason: str | None = None


def _flush_marker(account: MarketplaceAccount) -> str | None:
    flushed_at = account.last_feed_flush_at
    return flushed_at.isoformat() if flushed_at is not None else None


def _account_identity_marker(account: MarketplaceAccount) -> str:
    credentials = bytes(account.credentials_enc or b'')
    identity = b'\x00'.join([
        str(account.marketplace or '').encode('utf-8'),
        str(account.external_id or '').encode('utf-8'),
        credentials,
    ])
    return hashlib.sha256(identity).hexdigest()


def _marker_matches(
    account: MarketplaceAccount,
    expected_flush_marker: str | None,
) -> bool:
    return expected_flush_marker is None or _flush_marker(account) == expected_flush_marker


def _max_pages() -> int:
    return min(100, max(1, int(settings.AVITO_API_MAX_PAGES)))


def _bounded_delay(seconds: object) -> int:
    if isinstance(seconds, bool):
        return FEED_REPORT_PAGE_DELAY_SECONDS
    try:
        value = int(str(seconds))
    except (TypeError, ValueError):
        value = FEED_REPORT_PAGE_DELAY_SECONDS
    return min(FEED_REPORT_MAX_RETRY_DELAY_SECONDS, max(1, value))


def _retry_delay(attempt: int, *, requested: object | None = None) -> int:
    if requested is not None:
        return _bounded_delay(requested)
    return _bounded_delay(FEED_REPORT_PAGE_DELAY_SECONDS * (2 ** max(0, attempt)))


def _enqueue_page(
    *,
    account_id: int,
    page: int,
    expected_flush_marker: str | None,
    expected_account_marker: str | None,
    attempt: int,
    countdown: object,
):
    """Publish only scalar cursor state; report content never enters Celery."""

    return reconcile_avito_feed_item_errors_page_task.apply_async(
        kwargs={
            'account_id': account_id,
            'page': page,
            'expected_flush_marker': expected_flush_marker,
            'expected_account_marker': expected_account_marker,
            'attempt': attempt,
        },
        countdown=_bounded_delay(countdown),
    )


def schedule_avito_feed_item_error_reconciliation(
    account: MarketplaceAccount,
    *,
    countdown: int = FEED_REPORT_PAGE_DELAY_SECONDS,
):
    """Schedule page one after the caller has verified report freshness.

    The integration caller must first verify that Avito's
    ``last_successful`` upload belongs to ``account.last_feed_flush_at``.  The
    compact flush marker then stops all later pages if a newer local feed is
    uploaded while this scan is in progress.  An atomic cache reservation also
    coalesces repeated hooks for the same account/feed/credential generation.
    """

    if account.pk is None:
        raise ValueError('A persisted marketplace account is required')
    if account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
        raise ValueError('Feed item error reconciliation supports Avito only')
    marker = _flush_marker(account)
    if marker is None:
        raise ValueError('Cannot reconcile a feed report before the first feed flush')
    account_marker = _account_identity_marker(account)
    dedupe_digest = hashlib.sha256(
        f'{account.pk}:{marker}:{account_marker}'.encode('utf-8')
    ).hexdigest()
    dedupe_key = f'avito:feed-item-error-report:scheduled:{dedupe_digest}'
    if not coordination_cache.add(
        dedupe_key,
        1,
        timeout=FEED_REPORT_SCHEDULE_DEDUP_SECONDS,
    ):
        return None
    try:
        return _enqueue_page(
            account_id=account.pk,
            page=1,
            expected_flush_marker=marker,
            expected_account_marker=account_marker,
            attempt=0,
            countdown=countdown,
        )
    except Exception:
        # A failed broker publish must not consume this feed generation's
        # scheduling reservation.
        coordination_cache.delete(dedupe_key)
        raise


def _eligible_account(account_id: int) -> MarketplaceAccount | None:
    return (
        MarketplaceAccount.all_objects
        .select_related('tenant')
        .filter(
            pk=account_id,
            deleted_at__isnull=True,
            is_active=True,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            tenant__is_active=True,
        )
        .first()
    )


def _sanitize_reason(value: object) -> str:
    raw = html.unescape(str(value or ''))
    without_tags = re.sub(r'<[^>]+>', ' ', raw)
    without_controls = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', without_tags)
    normalized = re.sub(r'\s+', ' ', without_controls).strip()
    return normalized[:FEED_REPORT_REASON_MAX_LENGTH].rstrip()


def _normalized_errors(errors: dict[str, str]) -> dict[UUID, str]:
    if not isinstance(errors, dict) or len(errors) > FEED_REPORT_PAGE_SIZE:
        raise ValueError('Feed report page must contain at most 100 errors')
    normalized: dict[UUID, str] = {}
    for raw_ad_id, raw_reason in errors.items():
        if not isinstance(raw_ad_id, str):
            continue
        try:
            ad_id = UUID(raw_ad_id)
        except (ValueError, AttributeError):
            continue
        reason = _sanitize_reason(raw_reason)
        if reason:
            normalized[ad_id] = reason
    return normalized


def _lock_account(
    account_id: int,
    *,
    tenant_id: int,
) -> MarketplaceAccount | None:
    account = (
        MarketplaceAccount.all_objects
        .select_for_update(skip_locked=True, of=('self',))
        .filter(
            pk=account_id,
            tenant_id=tenant_id,
            deleted_at__isnull=True,
            is_active=True,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            tenant__is_active=True,
        )
        .first()
    )
    return account


def _feed_ingress_dual_write_enabled() -> bool:
    return settings.MARKETPLACE_FEED_INGRESS_MODE in {'dual_write', 'durable'}


class _StaleReportApply(RuntimeError):
    """Roll back a speculative page-level feed-intent advance."""


def _apply_page_errors(
    *,
    account_id: int,
    tenant_id: int,
    expected_flush_marker: str | None,
    expected_account_marker: str,
    errors: dict[str, str],
) -> _ApplyResult:
    errors_by_key = _normalized_errors(errors)
    changed_at = timezone.now()
    locked_tenant_id: int | None = None
    candidate_count = 0
    try:
        with transaction.atomic():
            account = _lock_account(
                account_id,
                tenant_id=tenant_id,
            )
            if account is None:
                return _ApplyResult(account_available=False)
            locked_tenant_id = account.tenant_id
            if not _marker_matches(account, expected_flush_marker):
                return _ApplyResult(
                    account_available=True,
                    tenant_id=account.tenant_id,
                    stale_reason='stale_feed',
                )
            if _account_identity_marker(account) != expected_account_marker:
                return _ApplyResult(
                    account_available=True,
                    tenant_id=account.tenant_id,
                    stale_reason='stale_account',
                )
            if not errors_by_key:
                return _ApplyResult(
                    account_available=True,
                    tenant_id=account.tenant_id,
                )

            candidates = Listing.all_objects.filter(
                account_id=account.pk,
                tenant_id=account.tenant_id,
                deleted_at__isnull=True,
                status=Listing.STATUS_PENDING,
                external_id__isnull=True,
                publish_idempotency_key__in=tuple(errors_by_key),
            )
            candidate_count = candidates.count()
            if candidate_count and _feed_ingress_dual_write_enabled():
                # One page is one account transaction.  Advance its desired
                # XML generation exactly once, before taking any Listing row
                # locks: account -> optional endpoint -> Listing.
                bump_feed_intents([account.pk], changed_at)

            listings = list(
                candidates.select_for_update(skip_locked=True, of=('self',))
                .order_by('pk')[:FEED_REPORT_PAGE_SIZE]
            )
            skipped_locked = max(0, candidate_count - len(listings))
            if candidate_count and not listings:
                # No projection membership actually changed.  Raising out of
                # the atomic block also rolls back the speculative page bump.
                raise _StaleReportApply

            changed_count = 0
            for listing in listings:
                reason = errors_by_key[listing.publish_idempotency_key]
                listing.status = Listing.STATUS_REJECTED
                listing.rejection_reason = reason
                listing.last_sync_at = changed_at
                listing.next_status_check_at = None
                listing.status_check_claim_token = None
                listing.status_check_claimed_until = None
                listing.updated_at = changed_at
                changed_count += 1

            if listings:
                Listing.all_objects.bulk_update(listings, [
                    'status',
                    'rejection_reason',
                    'last_sync_at',
                    'next_status_check_at',
                    'status_check_claim_token',
                    'status_check_claimed_until',
                    'updated_at',
                ])
                SyncLog.objects.bulk_create([
                    SyncLog(
                        tenant_id=listing.tenant_id,
                        listing_id=listing.pk,
                        event_type=SyncLog.EVENT_LISTING_ERROR,
                        status=SyncLog.STATUS_ERROR,
                        message=listing.rejection_reason,
                    )
                    for listing in listings
                ])
    except _StaleReportApply:
        return _ApplyResult(
            account_available=True,
            tenant_id=locked_tenant_id,
            skipped_locked=candidate_count,
        )

    return _ApplyResult(
        account_available=True,
        tenant_id=locked_tenant_id,
        changed_count=changed_count,
        skipped_locked=skipped_locked,
    )


def _notify_page_digest(
    *,
    tenant_id: int | None,
    account_id: int,
    expected_flush_marker: str | None,
    page: int,
    changed_count: int,
) -> None:
    """Emit at most one tenant notification for a report page.

    Per-listing details remain durable in ``SyncLog``. The bounded page digest
    prevents a 10k rejection report from creating 10k Telegram/Celery jobs.
    ``event_key`` makes redeliveries idempotent in NotificationDelivery.
    """

    if tenant_id is None or changed_count <= 0:
        return
    marker = hashlib.sha256(
        str(expected_flush_marker or 'unknown').encode('utf-8')
    ).hexdigest()[:24]
    send_notification_task.delay(
        tenant_id,
        LEVEL_ERROR,
        (
            f'Avito отклонил {changed_count} объявлений на странице {page} '
            'отчёта Autoload. Подробности доступны в логах.'
        ),
        event_key=f'avito-feed-report:{account_id}:{marker}:{page}',
    )


def _reschedule_same_page(
    *,
    account_id: int,
    page: int,
    expected_flush_marker: str | None,
    expected_account_marker: str | None,
    attempt: int,
    requested_delay: object | None = None,
) -> bool:
    if attempt >= FEED_REPORT_MAX_PAGE_ATTEMPTS:
        return False
    _enqueue_page(
        account_id=account_id,
        page=page,
        expected_flush_marker=expected_flush_marker,
        expected_account_marker=expected_account_marker,
        attempt=attempt + 1,
        countdown=_retry_delay(attempt, requested=requested_delay),
    )
    return True


@shared_task(
    name=(
        'apps.marketplaces.feed_report_reconciler.'
        'reconcile_avito_feed_item_errors_page_task'
    ),
    queue='avito_publish',
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_avito_feed_item_errors_page_task(
    account_id: int,
    page: int = 1,
    expected_flush_marker: str | None = None,
    expected_account_marker: str | None = None,
    attempt: int = 0,
):
    """Fetch and reconcile one report page, then enqueue only its next cursor."""

    if settings.AVITO_STATUS_LIFECYCLE_MODE != 'dual_write':
        return {'status': 'disabled'}
    if (
        isinstance(account_id, bool)
        or not isinstance(account_id, int)
        or account_id < 1
        or isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= _max_pages()
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or not 0 <= attempt <= FEED_REPORT_MAX_PAGE_ATTEMPTS
        or (
            expected_flush_marker is not None
            and (
                not isinstance(expected_flush_marker, str)
                or len(expected_flush_marker) > _FLUSH_MARKER_MAX_LENGTH
            )
        )
        or (
            expected_account_marker is not None
            and (
                not isinstance(expected_account_marker, str)
                or len(expected_account_marker) != 64
            )
        )
    ):
        return {'status': 'invalid_cursor'}

    lock_identity = f'avito:feed-item-error-report:{account_id}'
    with try_session_advisory_lock(lock_identity) as acquired:
        if not acquired:
            rescheduled = _reschedule_same_page(
                account_id=account_id,
                page=page,
                expected_flush_marker=expected_flush_marker,
                expected_account_marker=expected_account_marker,
                attempt=attempt,
            )
            return {'status': 'locked', 'rescheduled': rescheduled}

        account = _eligible_account(account_id)
        if account is None:
            return {'status': 'ineligible_account'}
        if not _marker_matches(account, expected_flush_marker):
            return {'status': 'stale_feed'}
        current_account_marker = _account_identity_marker(account)
        if (
            expected_account_marker is not None
            and current_account_marker != expected_account_marker
        ):
            return {'status': 'stale_account'}
        workflow_account_marker = expected_account_marker or current_account_marker

        try:
            page_result = AvitoAdapter(account).get_feed_item_error_page(page)
        except RateLimitError as exc:
            rescheduled = _reschedule_same_page(
                account_id=account_id,
                page=page,
                expected_flush_marker=expected_flush_marker,
                expected_account_marker=workflow_account_marker,
                attempt=attempt,
                requested_delay=exc.retry_after,
            )
            return {'status': 'provider_retry', 'rescheduled': rescheduled}
        except (AvitoError, requests.RequestException, ValueError, KeyError, TypeError):
            rescheduled = _reschedule_same_page(
                account_id=account_id,
                page=page,
                expected_flush_marker=expected_flush_marker,
                expected_account_marker=workflow_account_marker,
                attempt=attempt,
            )
            return {'status': 'provider_retry', 'rescheduled': rescheduled}

        if not isinstance(page_result, FeedItemErrorPage):
            return {'status': 'invalid_provider_page'}
        next_page = page_result.next_page
        if next_page is not None and next_page != page + 1:
            return {'status': 'invalid_provider_cursor'}

        try:
            applied = _apply_page_errors(
                account_id=account.pk,
                tenant_id=account.tenant_id,
                expected_flush_marker=expected_flush_marker,
                expected_account_marker=workflow_account_marker,
                errors=page_result.errors,
            )
        except ValueError:
            return {'status': 'invalid_provider_page'}

        if applied.stale_reason is not None:
            return {'status': applied.stale_reason}

        _notify_page_digest(
            tenant_id=applied.tenant_id,
            account_id=account_id,
            expected_flush_marker=expected_flush_marker,
            page=page,
            changed_count=applied.changed_count,
        )

        if not applied.account_available or applied.skipped_locked:
            rescheduled = _reschedule_same_page(
                account_id=account_id,
                page=page,
                expected_flush_marker=expected_flush_marker,
                expected_account_marker=workflow_account_marker,
                attempt=attempt,
            )
            return {
                'status': 'db_locked',
                'changed': applied.changed_count,
                'rescheduled': rescheduled,
            }

        scheduled_next = False
        stopped_at_limit = next_page is not None and page >= _max_pages()
        if next_page is not None and not stopped_at_limit:
            _enqueue_page(
                account_id=account_id,
                page=next_page,
                expected_flush_marker=expected_flush_marker,
                expected_account_marker=workflow_account_marker,
                attempt=0,
                countdown=FEED_REPORT_PAGE_DELAY_SECONDS,
            )
            scheduled_next = True

        return {
            'status': 'processed',
            'page': page,
            'changed': applied.changed_count,
            'scheduled_next': scheduled_next,
            'terminal': page_result.terminal or stopped_at_limit,
        }
