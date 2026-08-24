import datetime
import hashlib
import html
import logging
import uuid
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Protocol, cast

import requests
from celery import shared_task
from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now

from apps.anti_ban.ramp_up import GradualRampUp
from apps.anti_ban.velocity import VelocityController
from apps.billing.services import LimitChecker
from apps.core.advisory_lock import try_session_advisory_lock
from apps.core.dispatch import enqueue_durable_task
from apps.core.http_responses import TrustedResponseError
from apps.marketplaces.adapters.avito.adapter import (
    AmbiguousFeedSubmissionError,
    AvitoAdapter,
    FeedUploadError,
)
from apps.marketplaces.adapters.avito.error_handler import (
    AvitoError,
    ServerError,
    backoff,
)
from apps.marketplaces.adapters.avito.feed_builder import (
    build_feed,
    build_stop_feed,
    get_ad_id,
)
from apps.marketplaces.adapters.avito.rate_limiter import (
    AUTOLOAD_RATE_LIMIT_RETRY_AFTER,
    RateLimitError,
)
from apps.marketplaces.listing_lifecycle import (
    claim_status_check,
    clear_remote_observation,
    complete_claimed_status_check,
    normalize_remote_status,
    release_status_check,
)
from apps.marketplaces.feed_workflow import (
    FeedRunClaim,
    FeedRunConflict,
    FeedRunSnapshot,
    FeedSubmissionOutcomeUncertain,
    FeedWorkflowError,
    StaleFeedRunClaim,
    apply_poll_page,
    apply_report_page,
    cancel_feed_runs_for_inactive_owners,
    claim_due_run_for_account,
    create_or_supersede_feed_run,
    finish_feed_run,
    load_poll_batch,
    mark_feed_submission_unknown,
    mark_feed_submitted,
    persist_feed_submission_boundary,
    record_provider_run_observation,
    reset_poll_round,
    retry_step,
    start_reporting,
    validate_feed_submission_owner,
)
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.notifications.services import LEVEL_CRITICAL, LEVEL_ERROR, LEVEL_SUCCESS


cache = caches['coordination']
logger = logging.getLogger(__name__)


_STATUS_CHECK_CLAIM_LEASE = datetime.timedelta(minutes=5)
_ACTIVE_STATUS_RECHECK_DELAY = datetime.timedelta(hours=24)
_TRANSIENT_STATUS_RECHECK_DELAY = datetime.timedelta(minutes=10)
_POLL_RETRY_DELAY = datetime.timedelta(minutes=30)
_FEED_POLL_BATCH_SIZE = 100
_FEED_POLL_BATCH_DELAY_SECONDS = 30
_MAX_PROVIDER_REASON_LENGTH = 2000
_DURABLE_FEED_TASK_NAME = (
    'apps.marketplaces.tasks.process_marketplace_feed_run_step'
)
_DURABLE_FEED_RECOVERY_BATCH_SIZE = 100
_DURABLE_FEED_SUBMISSION_DELAY = datetime.timedelta(minutes=5)
_DURABLE_FEED_SUBMISSION_RECONCILE_HORIZON = datetime.timedelta(hours=2)
_DURABLE_FEED_SUBMISSION_NEGATIVE_THRESHOLD = 4
_DURABLE_FEED_REPORT_DELAY = datetime.timedelta(seconds=30)
_DURABLE_FEED_UPLOAD_CLOCK_SKEW = datetime.timedelta(minutes=5)
_MAX_DURABLE_FEED_PAYLOAD_LISTINGS = 10_000
_LISTING_EXPIRY_THRESHOLDS = (0, 1, 3, 7, 14)
_LISTING_EXPIRY_NOTICE_CACHE_MIN = datetime.timedelta(days=7)
_LISTING_EXPIRY_NOTICE_CACHE_MAX = datetime.timedelta(days=60)
_AVITO_REMOTE_STATUS_ALIASES = {
    # Avito calls an ad that has left active publication ``old``.
    'old': Listing.REMOTE_STATUS_ARCHIVED,
}


@dataclass(frozen=True, slots=True)
class _ListingStatusClaim:
    """Frozen local intent owned by one provider-read lease."""

    listing_id: int
    tenant_id: int
    account_id: int
    expected_marketplace: str
    expected_account_external_id: str
    expected_account_updated_at: datetime.datetime
    expected_status: str
    expected_external_id: str | None
    claim_token: uuid.UUID
    claimed_until: datetime.datetime


def _status_lifecycle_dual_write_enabled() -> bool:
    return settings.AVITO_STATUS_LIFECYCLE_MODE == 'dual_write'


def _durable_feed_run_enabled() -> bool:
    """Enable the durable feed owner only after lifecycle fencing is active."""

    return (
        settings.MARKETPLACE_FEED_RUN_MODE == 'durable'
        and _status_lifecycle_dual_write_enabled()
    )


def _bounded_provider_reason(value: object) -> str:
    """Return printable, single-line provider text safe for DB and notices."""

    text = str(value or '')
    printable = ''.join(character for character in text if character.isprintable())
    return ' '.join(printable.split())[:_MAX_PROVIDER_REASON_LENGTH]


def _provider_finish_time(value: object) -> datetime.datetime | None:
    """Parse Avito's bounded ISO finish_time in the configured local zone."""

    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


def _listing_expiry_days_left(
    finish_time: datetime.datetime,
    *,
    checked_at: datetime.datetime,
) -> int:
    seconds_left = (finish_time - checked_at).total_seconds()
    if seconds_left <= 0:
        return 0
    return int((seconds_left + 86399) // 86400)


def _queue_listing_expiry_notification(
    listing: Listing,
    response: dict,
    *,
    checked_at: datetime.datetime,
) -> None:
    """Warn once per Avito placement period and threshold without DB changes."""

    if str(response.get('status', '')).strip().lower() != 'active':
        return
    finish_time = _provider_finish_time(response.get('finish_time'))
    if finish_time is None:
        return

    days_left = _listing_expiry_days_left(finish_time, checked_at=checked_at)
    threshold = next(
        (
            value
            for value in _LISTING_EXPIRY_THRESHOLDS
            if days_left <= value
        ),
        None,
    )
    if threshold is None:
        return

    finish_utc = finish_time.astimezone(datetime.timezone.utc)
    period_key = finish_utc.strftime('%Y%m%dT%H%M%SZ')
    event_key = f'avito-listing-expiry:{listing.pk}:{period_key}:{threshold}'
    cache_key = f'notice:{event_key}'
    cache_lifetime = finish_time - checked_at + datetime.timedelta(days=7)
    cache_lifetime = max(cache_lifetime, _LISTING_EXPIRY_NOTICE_CACHE_MIN)
    cache_lifetime = min(cache_lifetime, _LISTING_EXPIRY_NOTICE_CACHE_MAX)

    try:
        should_queue = cache.add(
            cache_key,
            '1',
            timeout=max(1, int(cache_lifetime.total_seconds())),
        )
    except Exception:
        # Delivery itself has a durable event key. Cache loss may enqueue a
        # duplicate task, but cannot create a duplicate channel delivery.
        logger.exception(
            'Failed to coalesce Avito expiry notice listing_id=%s',
            listing.pk,
        )
        should_queue = True
    if not should_queue:
        return

    listing_label = ' '.join(
        str(listing.title or listing.product.name or f'#{listing.pk}').split()
    )[:160]
    finish_local = timezone.localtime(finish_time)
    finish_label = finish_local.strftime('%d.%m.%Y %H:%M')
    if days_left == 0:
        level = LEVEL_CRITICAL
        message = (
            f'Avito ({listing.account.name}): срок размещения объявления '
            f'«{listing_label}» закончился {finish_label}, но API пока '
            'возвращает статус active. Проверьте объявление в Avito; MAP '
            'продолжит сверку автоматически.'
        )
    else:
        level = LEVEL_CRITICAL if days_left <= 1 else LEVEL_ERROR
        message = (
            f'Avito ({listing.account.name}): объявление «{listing_label}» '
            f'активно до {finish_label} — осталось {days_left} дн. '
            'Проверьте продление или повторное размещение заранее, если '
            'объявление должно оставаться активным.'
        )

    from apps.notifications.tasks import send_notification_task

    try:
        send_notification_task.delay(
            listing.tenant_id,
            level,
            message,
            {
                'account_id': listing.account_id,
                'listing_id': listing.pk,
                'finish_time': finish_time.isoformat(),
                'days_left': days_left,
            },
            event_key=event_key,
        )
    except Exception:
        # A broker outage must not turn a successful provider read into a
        # failed moderation check. Release the coalescing key so the next
        # scheduled check can retry dispatching the notice.
        try:
            cache.delete(cache_key)
        except Exception:
            logger.exception(
                'Failed to release Avito expiry notice cache listing_id=%s',
                listing.pk,
            )
        logger.exception(
            'Failed to enqueue Avito expiry notice listing_id=%s',
            listing.pk,
        )


def _claim_listing_status_check(
    listing_id: int,
    *,
    eligible_statuses: tuple[str, ...],
    require_external_id: bool,
) -> tuple[_ListingStatusClaim | None, str]:
    """Claim one live row and freeze the provider identity used by the call."""

    account_id = Listing.objects.filter(pk=listing_id).values_list(
        'account_id', flat=True,
    ).first()
    if account_id is None:
        return None, 'stale_intent'

    claim_time = now()
    with transaction.atomic():
        account = (
            MarketplaceAccount.objects.select_for_update(of=('self',))
            .only('pk', 'tenant_id', 'marketplace', 'external_id', 'updated_at')
            .filter(
                pk=account_id,
                is_active=True,
                tenant__is_active=True,
                marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            )
            .exclude(external_id='')
            .first()
        )
        if account is None:
            return None, 'inactive_account'
        listing = (
            Listing.objects.select_for_update(of=('self',))
            .only(
                'pk', 'account_id', 'status', 'external_id',
                'next_status_check_at', 'status_check_claim_token',
                'status_check_claimed_until',
            )
            .filter(
                pk=listing_id,
                tenant_id=account.tenant_id,
                account_id=account.pk,
            )
            .first()
        )
        if listing is None or listing.status not in eligible_statuses:
            return None, 'stale_intent'
        if require_external_id and not listing.external_id:
            return None, 'missing_external_id'
        if (
            listing.next_status_check_at is not None
            and listing.next_status_check_at > claim_time
        ):
            return None, 'not_due'
        if (
            listing.status_check_claim_token is not None
            and listing.status_check_claimed_until is not None
            and listing.status_check_claimed_until > claim_time
        ):
            return None, 'already_claimed'

        token = uuid.uuid4()
        claimed_until = claim_time + _STATUS_CHECK_CLAIM_LEASE
        claim_fields = claim_status_check(
            claim_token=token,
            claimed_until=claimed_until,
        ).apply_to(listing)
        listing.save(update_fields=claim_fields)
        return _ListingStatusClaim(
            listing_id=listing.pk,
            tenant_id=account.tenant_id,
            account_id=listing.account_id,
            expected_marketplace=account.marketplace,
            expected_account_external_id=account.external_id,
            expected_account_updated_at=account.updated_at,
            expected_status=listing.status,
            expected_external_id=listing.external_id,
            claim_token=token,
            claimed_until=claimed_until,
        ), ''


def _claim_pending_feed_rows(account_id: int) -> list[_ListingStatusClaim]:
    """Claim at most one bounded batch of ready PENDING rows."""

    claim_time = now()
    claimed_until = claim_time + _STATUS_CHECK_CLAIM_LEASE
    token = uuid.uuid4()
    available_claim = (
        Q(status_check_claim_token__isnull=True)
        | Q(status_check_claimed_until__isnull=True)
        | Q(status_check_claimed_until__lte=claim_time)
    )
    with transaction.atomic():
        account = (
            MarketplaceAccount.objects.select_for_update(of=('self',))
            .only('pk', 'tenant_id', 'marketplace', 'external_id', 'updated_at')
            .filter(
                pk=account_id,
                is_active=True,
                tenant__is_active=True,
                marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            )
            .exclude(external_id='')
            .first()
        )
        if account is None:
            return []
        candidates = list(
            Listing.objects.select_for_update(skip_locked=True, of=('self',))
            .filter(
                available_claim,
                tenant_id=account.tenant_id,
                account_id=account_id,
                status=Listing.STATUS_PENDING,
                external_id__isnull=True,
            )
            .filter(
                Q(next_status_check_at__isnull=True)
                | Q(next_status_check_at__lte=claim_time),
            )
            .only('pk', 'account_id', 'status', 'external_id')
            .order_by(F('next_status_check_at').asc(nulls_first=True), 'pk')
            [:_FEED_POLL_BATCH_SIZE]
        )
        if not candidates:
            return []
        Listing.objects.filter(
            pk__in=[listing.pk for listing in candidates],
        ).update(**claim_status_check(
            claim_token=token,
            claimed_until=claimed_until,
        ).as_update_kwargs())

    return [
        _ListingStatusClaim(
            listing_id=listing.pk,
            tenant_id=account.tenant_id,
            account_id=listing.account_id,
            expected_marketplace=account.marketplace,
            expected_account_external_id=account.external_id,
            expected_account_updated_at=account.updated_at,
            expected_status=listing.status,
            expected_external_id=listing.external_id,
            claim_token=token,
            claimed_until=claimed_until,
        )
        for listing in candidates
    ]


def _claimed_listing_queryset(
    claim: _ListingStatusClaim,
    *,
    lease_checked_at: datetime.datetime,
):
    """Build the exact local-intent, account-identity and lease CAS fence."""

    return Listing.all_objects.filter(
        pk=claim.listing_id,
        deleted_at__isnull=True,
        tenant_id=claim.tenant_id,
        tenant__is_active=True,
        account_id=claim.account_id,
        account__deleted_at__isnull=True,
        account__is_active=True,
        account__marketplace=claim.expected_marketplace,
        account__external_id=claim.expected_account_external_id,
        account__updated_at=claim.expected_account_updated_at,
        status=claim.expected_status,
        external_id=claim.expected_external_id,
        status_check_claim_token=claim.claim_token,
        status_check_claimed_until__gt=lease_checked_at,
    )


def _lock_claim_account(claim: _ListingStatusClaim) -> bool:
    return (
        MarketplaceAccount.objects.select_for_update(of=('self',))
        .only('pk')
        .filter(
            pk=claim.account_id,
            tenant_id=claim.tenant_id,
            tenant__is_active=True,
            marketplace=claim.expected_marketplace,
            external_id=claim.expected_account_external_id,
            updated_at=claim.expected_account_updated_at,
            is_active=True,
        )
        .first()
        is not None
    )


def _min_nudge_account_status_due(
    claim: _ListingStatusClaim,
    due_at: datetime.datetime | None,
) -> int:
    if due_at is None:
        return 0
    return MarketplaceAccount.objects.filter(
        pk=claim.account_id,
        tenant_id=claim.tenant_id,
        tenant__is_active=True,
        marketplace=claim.expected_marketplace,
        external_id=claim.expected_account_external_id,
        updated_at=claim.expected_account_updated_at,
        is_active=True,
    ).filter(
        Q(status_batch_due_at__isnull=True) | Q(status_batch_due_at__gt=due_at),
    ).update(status_batch_due_at=due_at)


def _apply_claimed_listing_values(
    claim: _ListingStatusClaim,
    *,
    values: dict[str, object],
    next_status_check_at: datetime.datetime | None,
    nudge_status_due: bool,
) -> int:
    """Apply a provider result only if its frozen local intent is still live."""

    with transaction.atomic():
        if not _lock_claim_account(claim):
            return 0
        affected = _claimed_listing_queryset(
            claim,
            lease_checked_at=now(),
        ).update(**values)
        if affected == 1 and nudge_status_due:
            _min_nudge_account_status_due(claim, next_status_check_at)
        return affected


def _apply_claimed_status_result(
    claim: _ListingStatusClaim,
    *,
    raw_remote_status: object,
    checked_at: datetime.datetime,
    next_status_check_at: datetime.datetime | None,
    canonical_updates: dict[str, object] | None = None,
) -> int:
    lifecycle = complete_claimed_status_check(
        raw_remote_status,
        checked_at=checked_at,
        next_status_check_at=next_status_check_at,
        aliases=_AVITO_REMOTE_STATUS_ALIASES,
    )
    values = lifecycle.as_update_kwargs()
    values.update(canonical_updates or {})
    return _apply_claimed_listing_values(
        claim,
        values=values,
        next_status_check_at=next_status_check_at,
        nudge_status_due=True,
    )


def _release_status_claim(
    claim: _ListingStatusClaim,
    *,
    next_status_check_at: datetime.datetime | None,
    nudge_account: bool = True,
) -> int:
    with transaction.atomic():
        if not _lock_claim_account(claim):
            return 0
        affected = _claimed_listing_queryset(
            claim,
            lease_checked_at=now(),
        ).update(**release_status_check(
            next_status_check_at=next_status_check_at,
        ).as_update_kwargs())
        if affected == 1 and nudge_account:
            _min_nudge_account_status_due(claim, next_status_check_at)
        return affected


def _release_status_claims(
    claims: list[_ListingStatusClaim],
    *,
    next_status_check_at: datetime.datetime | None,
    nudge_account: bool = True,
) -> int:
    return sum(
        _release_status_claim(
            claim,
            next_status_check_at=next_status_check_at,
            nudge_account=nudge_account,
        )
        for claim in claims
    )


def _load_owned_claimed_listings(
    claims: list[_ListingStatusClaim],
) -> list[tuple[_ListingStatusClaim, Listing]]:
    if not claims:
        return []
    claim_by_id = {claim.listing_id: claim for claim in claims}
    loaded = {
        listing.pk: listing
        for listing in Listing.objects.select_related('tenant', 'product', 'account')
        .filter(pk__in=claim_by_id)
    }
    result = []
    checked_at = now()
    for claim in claims:
        listing = loaded.get(claim.listing_id)
        if listing is None:
            continue
        if (
            not listing.tenant.is_active
            or listing.account_id != claim.account_id
            or listing.tenant_id != claim.tenant_id
            or listing.account.marketplace != claim.expected_marketplace
            or listing.account.external_id != claim.expected_account_external_id
            or listing.account.updated_at != claim.expected_account_updated_at
            or not listing.account.is_active
            or listing.account.deleted_at is not None
            or listing.status != claim.expected_status
            or listing.external_id != claim.expected_external_id
            or listing.status_check_claim_token != claim.claim_token
            or listing.status_check_claimed_until is None
            or listing.status_check_claimed_until <= checked_at
        ):
            continue
        result.append((claim, listing))
    return result


class _CoordinationLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def acquire(self, blocking: bool = True) -> bool: ...

    def release(self) -> None: ...


class _LockingCache(Protocol):
    def lock(self, key: str, *, timeout: int) -> _CoordinationLock: ...


def _coordination_lock(key: str, *, timeout: int) -> _CoordinationLock:
    """Typed view of the django-redis coordination backend contract."""
    return cast(_LockingCache, cache).lock(key, timeout=timeout)


def _notify_error(tenant, message: str, listing=None) -> None:
    """Отправляет error-уведомление тенанту и записывает SyncLog с STATUS_ERROR."""
    from apps.notifications.tasks import send_notification_task
    from apps.sync.models import SyncLog

    send_notification_task.delay(tenant.pk, LEVEL_ERROR, message)
    SyncLog.objects.create(
        tenant=tenant,
        listing=listing,
        event_type=SyncLog.EVENT_LISTING_ERROR,
        status=SyncLog.STATUS_ERROR,
        message=message,
    )


def _write_log(tenant, event_type: str, status: str, message: str, listing=None) -> None:
    """Записывает событие в SyncLog — не падает при ошибках."""
    try:
        from apps.sync.models import SyncLog
        SyncLog.objects.create(
            tenant=tenant,
            event_type=event_type,
            status=status,
            message=message,
            listing=listing,
        )
    except Exception:
        pass


def _notify_critical(tenant, message: str) -> None:
    """Асинхронно отправляет critical-уведомление тенанту."""
    from apps.notifications.tasks import send_notification_task
    send_notification_task.delay(tenant.pk, LEVEL_CRITICAL, message)


def _notify_success(tenant, message: str, listing=None) -> None:
    """Асинхронно отправляет success-уведомление и пишет ok SyncLog."""
    from apps.notifications.tasks import send_notification_task
    from apps.sync.models import SyncLog

    send_notification_task.delay(tenant.pk, LEVEL_SUCCESS, message)
    SyncLog.objects.create(
        tenant=tenant,
        listing=listing,
        event_type=SyncLog.EVENT_LISTING_PUBLISH,
        status=SyncLog.STATUS_OK,
        message=message,
    )


def _get_listing(listing_id: int) -> Listing:
    return Listing.objects.select_related('tenant', 'product', 'account').get(pk=listing_id)


def _merged_update_fields(*field_groups) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        field
        for fields in field_groups
        for field in fields
    ))


def _local_status_due_at(listing: Listing) -> datetime.datetime | None:
    if not listing.external_id:
        return None
    if listing.status in {
        Listing.STATUS_PENDING,
        Listing.STATUS_ARCHIVING,
        Listing.STATUS_ACTIVE,
    }:
        return now() + _TRANSIENT_STATUS_RECHECK_DELAY
    return None


def _copy_listing_row(target: Listing, source: Listing) -> None:
    for model_field in Listing._meta.concrete_fields:
        setattr(target, model_field.attname, getattr(source, model_field.attname))
    target._state.fields_cache.clear()


def _save_local_listing_intent(
    listing: Listing,
    *,
    update_fields: tuple[str, ...] | list[str],
    reset_provider_identity: bool = False,
    expected_status: str | None = None,
    expected_external_id: str | None | object = ...,
) -> bool:
    """Persist a local intent and revoke an older in-flight provider read."""

    business_fields = tuple(update_fields)
    if not _status_lifecycle_dual_write_enabled():
        listing.save(update_fields=business_fields)
        return True

    intended_values: dict[str, object] = {}
    for field_name in business_fields:
        intended_values[field_name] = getattr(listing, field_name)
    if reset_provider_identity:
        listing.external_url = ''
        intended_values['external_url'] = ''
        business_fields = _merged_update_fields(
            business_fields,
            ('external_url',),
        )

    def _matches_expected(current: Listing) -> bool:
        if current.account_id != listing.account_id:
            return False
        if expected_status is not None and current.status != expected_status:
            return False
        if (
            expected_external_id is not ...
            and current.external_id != expected_external_id
        ):
            return False
        return True

    with transaction.atomic():
        account = (
            MarketplaceAccount.all_objects.select_for_update(of=('self',))
            .filter(
                pk=listing.account_id,
                deleted_at__isnull=True,
                is_active=True,
                tenant__is_active=True,
            )
            .first()
        )
        if account is None:
            return False
        current = (
            Listing.all_objects.select_for_update(of=('self',))
            .filter(pk=listing.pk, deleted_at__isnull=True)
            .first()
        )
        if current is None or not _matches_expected(current):
            if current is not None:
                _copy_listing_row(listing, current)
            return False

        for field_name, value in intended_values.items():
            setattr(current, field_name, value)

        if reset_provider_identity:
            observation_fields = clear_remote_observation().apply_to(current)
            claim_fields = release_status_check(
                next_status_check_at=None,
            ).apply_to(current)
            lifecycle_fields = _merged_update_fields(
                observation_fields,
                claim_fields,
            )
        else:
            lifecycle_fields = release_status_check(
                next_status_check_at=_local_status_due_at(current),
            ).apply_to(current)

        saved_fields = _merged_update_fields(business_fields, lifecycle_fields)
        current.save(update_fields=saved_fields)
        for field_name in saved_fields:
            setattr(listing, field_name, getattr(current, field_name))
        listing._state.fields_cache.clear()

        due_at = current.next_status_check_at
        if due_at is not None:
            MarketplaceAccount.objects.filter(pk=account.pk).filter(
                Q(status_batch_due_at__isnull=True)
                | Q(status_batch_due_at__gt=due_at),
            ).update(status_batch_due_at=due_at)
        return True


def _reject_listing(listing: Listing, reason: str) -> None:
    if _status_lifecycle_dual_write_enabled():
        reason = _bounded_provider_reason(reason)
    expected_status = listing.status
    expected_external_id = listing.external_id
    listing.status = Listing.STATUS_REJECTED
    listing.rejection_reason = reason
    listing.last_sync_at = now()
    saved = _save_local_listing_intent(
        listing,
        update_fields=('status', 'rejection_reason', 'last_sync_at'),
        expected_status=expected_status,
        expected_external_id=expected_external_id,
    )
    if saved:
        _notify_error(listing.tenant, reason, listing=listing)


def _send_listing_to_review(listing: Listing, reason: str) -> None:
    """Отправляет листинг на проверку (вкладка «Требуют проверки») с причиной.

    В отличие от _reject_listing статус — requires_review: тенант исправляет
    данные и жмёт «Одобрить и опубликовать» (ListingService.approve).
    """
    expected_status = listing.status
    expected_external_id = listing.external_id
    listing.status = Listing.STATUS_REQUIRES_REVIEW
    listing.rejection_reason = reason
    listing.last_sync_at = now()
    saved = _save_local_listing_intent(
        listing,
        update_fields=('status', 'rejection_reason', 'last_sync_at'),
        expected_status=expected_status,
        expected_external_id=expected_external_id,
    )
    if saved:
        _notify_error(listing.tenant, reason, listing=listing)


# Пауза перед повтором при лимите Avito «1 автозагрузка/час» (~11 минут).
RATE_LIMIT_RETRY_COUNTDOWN = AUTOLOAD_RATE_LIMIT_RETRY_AFTER


def _account_feed_listings(
    account: MarketplaceAccount,
    *,
    limit: int | None = None,
) -> list[Listing]:
    """
    Полное состояние фида аккаунта в одной автозагрузке (фид-координатор):
    ВСЕ объявления, которые должны быть активны (active/pending/queued).

    Avito тянет один URL фида на аккаунт. Снятие делается ОТСУТСТВИЕМ объявления
    в файле (Avito архивирует то, чего нет), поэтому archived/deleted сюда НЕ
    включаем — они уйдут в архив на стороне Avito.
    """
    queryset = (
        Listing.objects.filter(
            account=account,
            status__in=[
                Listing.STATUS_ACTIVE,
                Listing.STATUS_PENDING,
                Listing.STATUS_QUEUED,
            ],
        )
        .select_related('tenant', 'product', 'account')
        .order_by('created_at', 'pk')
    )
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be a positive integer.')
        queryset = queryset[:limit]
    return list(queryset)


def _flush_account_or_stop(account) -> None:
    """
    Загружает в Avito актуальное состояние аккаунта одной автозагрузкой.

    Есть активные объявления — отдаём их фид (отсутствующие Avito архивирует).
    Активных не осталось — отдаём команду STOP (снять все), т.к. пустой фид
    Avito снятием не считает.
    """
    listings = _account_feed_listings(account)
    adapter = AvitoAdapter(account)
    if listings:
        adapter.flush_feed(listings)
    else:
        adapter.flush_stop()


# Avito читает автозагрузку ~раз в час. Изменения тенанта между окнами копятся
# и уходят одним фидом — см. request_feed_flush / coalesced_flush_task.
FEED_WINDOW_SECONDS = 3600


def _feed_window_remaining(account) -> int:
    """Секунд до открытия окна автозагрузки. 0 — окно открыто, можно слать фид."""
    if not account.last_feed_flush_at:
        return 0
    elapsed = (now() - account.last_feed_flush_at).total_seconds()
    return max(0, int(FEED_WINDOW_SECONDS - elapsed))


def request_feed_flush(account) -> None:
    """
    Координатор часового окна автозагрузки (каденс «первый сразу, дальше копим»).

    Окно открыто → flush сразу. Окно закрыто → копим: гарантируем, что ровно один
    отложенный flush запланирован на момент открытия (debounce через cache-маркер,
    чтобы десятки действий тенанта не наплодили задач).
    """
    remaining = _feed_window_remaining(account)
    if remaining == 0:
        coalesced_flush_task.delay(account.pk)
        return
    marker = f'avito:flush_scheduled:{account.pk}'
    if cache.add(marker, 1, timeout=remaining + 60):
        coalesced_flush_task.apply_async(args=[account.pk], countdown=remaining)


def _validate_feed_batch(listings: list) -> list:
    """
    Отсеивает из партии объявления, которые Avito точно не примет, с понятным
    пояснением тенанту; по «мягким» проблемам (нет OEM, незаполненные поля) только
    предупреждает. Возвращает валидную часть партии.
    """
    from apps.marketplaces.adapters.avito.feed_builder import (
        AVITO_SUBTYPE_LABELS, blocking_missing_avito_fields, get_contact_fields,
        format_avito_field_requirements, has_resolved_category,
        missing_required_avito_fields, product_brand_is_missing, product_has_oem,
        unknown_brand_details,
    )
    valid = []
    catalog_refresh_attempted = False
    for item in listings:
        manager_name, contact_phone = get_contact_fields(item)
        if not manager_name or not contact_phone:
            _reject_listing(
                item,
                'Не указано контактное лицо и/или телефон. Заполните их в профиле '
                'аккаунта Avito (Настройки → Маркетплейсы) или в самом листинге — '
                'Avito не публикует объявления без контактов.',
            )
            continue
        # Производитель обязателен для новых запчастей и валидируется по каталогу
        # Avito: пустой бренд уходил фолбэком «имя тенанта» и отклонялся с
        # непонятным «Значение не найдено». Отсекаем сразу с понятной причиной.
        if product_brand_is_missing(item):
            _reject_listing(
                item,
                'У товара не указан производитель. Для новой запчасти это обязательное '
                'поле: без него Avito отклонит объявление. Укажите производителя '
                'в карточке товара, проверьте написание по справочнику Avito и '
                'опубликуйте объявление снова.',
            )
            continue
        # Бренд, которого нет в каталоге Avito → на проверку тенанту, остальная
        # партия публикуется без задержки. Устаревший справочник один раз за партию
        # обновляем вне расписания и затем проверяем бренд повторно.
        unknown_brand = unknown_brand_details(item)
        if unknown_brand is not None and not catalog_refresh_attempted:
            from apps.marketplaces.adapters.avito.brand_catalog import catalog_status
            if catalog_status()['stale']:
                catalog_refresh_attempted = True
                try:
                    from apps.marketplaces.adapters.avito.brand_sync import sync_brand_catalog
                    sync_brand_catalog(item.account)
                except Exception as exc:
                    _write_log(
                        item.tenant, 'listing_publish', 'warn',
                        f'Не удалось обновить справочник брендов Avito: {exc}. '
                        'Используется последняя рабочая версия.',
                        listing=item,
                    )
                unknown_brand = unknown_brand_details(item)
        if unknown_brand is not None:
            brand, suggestions = unknown_brand
            hint = ''
            if suggestions:
                variants = ', '.join(f'«{suggestion}»' for suggestion in suggestions)
                hint = (
                    f' В справочнике есть похожее название: {variants}. Выбирайте его '
                    'только в том случае, если это действительно тот же производитель.'
                )
            _send_listing_to_review(
                item,
                f'Avito не распознал производителя «{brand}». Для новой запчасти '
                f'объявление с таким значением будет отклонено. Проверьте написание '
                f'производителя в карточке товара.{hint} Если название указано верно, '
                f'обратитесь в поддержку Avito с просьбой добавить производителя '
                f'в справочник. Остальные объявления продолжают публиковаться.',
            )
            continue
        # Под-вид детали (Подкатегория 3) обязателен для листьев Двигатель/Кузов/
        # Трансмиссия — без него Avito отклоняет объявление. Отсекаем сразу
        # с понятной причиной, а не постфактум из отчёта автозагрузки.
        blocking = blocking_missing_avito_fields(item)
        if blocking:
            labels = ', '.join(f'«{AVITO_SUBTYPE_LABELS[tag]}»' for tag in blocking)
            category = getattr(item.product, 'catalog_category', None)
            category_name = getattr(category, 'name', '') or 'категории товара'
            _reject_listing(
                item,
                f'Для категории «{category_name}» нужно точнее указать вид детали: '
                f'{labels}. Без этого Avito отклонит объявление. Откройте листинг, '
                f'в поле «Категория Avito» выберите конечную подкатегорию и '
                f'опубликуйте объявление снова.',
            )
            continue
        # Категория не определена → фид уйдёт с дефолтной Avito-категорией и часто
        # отклоняется («ошибка описания»). Раньше тут была тишина — предупреждаем.
        if not has_resolved_category(item):
            _write_log(
                item.tenant, 'listing_publish', 'warn',
                f'У «{item.title or item.product.name}» не определена категория — '
                f'Avito может отклонить объявление («ошибка описания»). Укажите категорию у товара.',
                listing=item,
            )
        if not product_has_oem(item):
            _write_log(
                item.tenant, 'listing_publish', 'warn',
                f'У «{item.title or item.product.name}» нет OEM-номера — в объявление '
                f'подставлен артикул {item.product.article}. Укажите OEM вручную при необходимости.',
                listing=item,
            )
        missing_fields = missing_required_avito_fields(item)
        if missing_fields:
            readable_fields = format_avito_field_requirements(missing_fields)
            _write_log(
                item.tenant, 'listing_publish', 'warn',
                f'Для товара «{item.title or item.product.name}» Avito требует '
                f'дополнительные характеристики: {readable_fields}. Без них объявление '
                f'может быть отклонено. Передайте значения администратору или в '
                f'поддержку MAP для настройки выгрузки.',
                listing=item,
            )
        valid.append(item)
    return valid


@shared_task(name='apps.marketplaces.tasks.sync_avito_brand_catalog')
def sync_avito_brand_catalog_task():
    """Обновляет общий справочник; при ошибке старая версия остаётся в БД."""
    from apps.marketplaces.adapters.avito.brand_sync import sync_brand_catalog
    catalog = sync_brand_catalog()
    return {'count': len(catalog.brands), 'synced_at': catalog.synced_at.isoformat()}


@shared_task(bind=True, max_retries=6, queue='avito_publish')
def publish_listing_task(self, listing_id: int):
    """
    Проверяет лимиты и загружает листинг в Avito через Autoload-фид.

    Avito обрабатывает фид асинхронно: external_id придёт через poll_feed_results_task.
    Статус после успешной загрузки фида: PENDING (ждёт обработки Avito).
    """
    listing = _get_listing(listing_id)

    lock_key = f'avito:publish_lock:{listing.publish_idempotency_key}'
    with _coordination_lock(lock_key, timeout=60):
        listing.refresh_from_db()
        # Публикуем только из публикуемых статусов; активные/ожидающие отсекаются
        # здесь же (это и есть защита от повторной публикации живого объявления).
        # limit_reached публикуем повторно: лимит перепроверяется ниже, а после
        # продления подписки листинг не должен застревать в этом статусе.
        if listing.status not in (
            Listing.STATUS_QUEUED, Listing.STATUS_DRAFT,
            Listing.STATUS_REJECTED, Listing.STATUS_LIMIT_REACHED,
        ):
            return
        # Повторная публикация из архива: старый external_id неактуален (объявление
        # было снято). Сбрасываем его — иначе раньше задача молча выходила по
        # `if listing.external_id: return`, и листинг навсегда висел «в очереди».
        if listing.external_id:
            expected_external_id = listing.external_id
            listing.external_id = None
            if not _save_local_listing_intent(
                listing,
                update_fields=('external_id',),
                reset_provider_identity=True,
                expected_status=listing.status,
                expected_external_id=expected_external_id,
            ):
                return {'status': 'stale'}
        if listing.status != Listing.STATUS_QUEUED:
            expected_status = listing.status
            listing.status = Listing.STATUS_QUEUED
            if not _save_local_listing_intent(
                listing,
                update_fields=('status',),
                expected_status=expected_status,
                expected_external_id=listing.external_id,
            ):
                return {'status': 'stale'}

        can, reason = LimitChecker().can_publish(listing.tenant)
        if not can:
            expected_status = listing.status
            listing.status = Listing.STATUS_LIMIT_REACHED
            saved = _save_local_listing_intent(
                listing,
                update_fields=('status',),
                expected_status=expected_status,
                expected_external_id=listing.external_id,
            )
            if saved:
                _notify_critical(
                    listing.tenant,
                    f'Достигнут лимит публикаций: {reason}. Новые объявления заблокированы.',
                )
            return

        ramp = GradualRampUp()
        published_today = ramp.get_published_today(listing.tenant)
        if not ramp.is_allowed(listing.tenant, published_today):
            raise self.retry(exc=RuntimeError('Ramp-up limit reached'), countdown=3600)

        if not VelocityController().is_allowed(listing.account, 'publish'):
            raise self.retry(exc=RuntimeError('Velocity limit exceeded'), countdown=300)

        # Avito требует контакты — без них фид точно отклонят. Отсекаем сразу.
        if not _validate_feed_batch([listing]):
            return

        # Помечаем «На модерации Авито» и отдаём фид координатору часового окна:
        # сам триггер автозагрузки произойдёт в coalesced_flush_task (первый —
        # сразу, последующие за час — копятся и уходят одним фидом).
        listing.status = Listing.STATUS_PENDING
        if not _save_local_listing_intent(
            listing,
            update_fields=('status',),
            expected_status=Listing.STATUS_QUEUED,
            expected_external_id=None,
        ):
            return {'status': 'stale'}
        _write_log(
            listing.tenant, 'listing_publish', 'ok',
            f'«{listing.title or listing.product.name}» принято к публикации — на модерации Avito',
            listing=listing,
        )

    request_feed_flush(listing.account)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='avito_publish')
def requeue_limit_reached_listings(self, tenant_id: int):
    """
    Повторно публикует листинги тенанта, упёршиеся в лимит подписки.

    Запускается после активации/продления подписки: статус «Лимит достигнут»
    сам по себе не рассасывается, а publish_listing_task заново проверит
    лимиты — если план всё ещё не позволяет, листинг вернётся в limit_reached.
    """
    listing_ids = list(
        Listing.objects.filter(
            tenant_id=tenant_id, status=Listing.STATUS_LIMIT_REACHED,
        ).values_list('pk', flat=True)
    )
    for listing_id in listing_ids:
        publish_listing_task.delay(listing_id)
    return {'requeued': len(listing_ids)}


@shared_task(bind=True, max_retries=6, queue='avito_update')
def update_listing_task(self, listing_id: int):
    """Обновляет содержимое объявления — пересборка фида по часовому окну."""
    listing = _get_listing(listing_id)
    if not listing.external_id:
        publish_listing_task.delay(listing_id)
        return
    listing.last_sync_at = now()
    listing.save(update_fields=['last_sync_at'])
    _write_log(
        listing.tenant, 'listing_update', 'ok',
        f'Изменения «{listing.title or listing.product.name}» уйдут в Avito ближайшим фидом',
        listing=listing,
    )
    request_feed_flush(listing.account)


@shared_task(bind=True, max_retries=5, queue='avito_price')
def update_price_task(self, listing_id: int):
    """
    Обновляет цену через REST API: POST /core/v1/items/{item_id}/update_price.

    Единственная write-операция Avito, доступная без фида.
    """
    listing = _get_listing(listing_id)
    if not listing.external_id:
        return
    try:
        AvitoAdapter(listing.account).update_price(listing)
        listing.last_sync_at = now()
        listing.save(update_fields=['last_sync_at'])
        _write_log(
            listing.tenant, 'listing_price_update', 'ok',
            f'Цена «{listing.title or listing.product.name}» обновлена: {listing.price_on_listing}₽',
            listing=listing,
        )
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=6, queue='avito_delete')
def unpublish_listing_task(self, listing_id: int):
    """Снимает объявление с публикации (удаление из фида Avito)."""
    listing = _get_listing(listing_id)
    if not listing.external_id:
        # Никогда не публиковалось на Avito — снимать нечего, сразу в архив.
        expected_status = listing.status
        listing.status = Listing.STATUS_ARCHIVED
        saved = _save_local_listing_intent(
            listing,
            update_fields=('status',),
            expected_status=expected_status,
            expected_external_id=None,
        )
        return {'status': 'archived' if saved else 'stale'}
    # Промежуточный статус «Снимается» — в «В архиве» переведём после подтверждения.
    # Снятие = отсутствие объявления в фиде; фид уйдёт ближайшим часовым окном,
    # а check_moderation_status дожмёт подтверждение снятия (confirm_removal_task).
    expected_status = listing.status
    expected_external_id = listing.external_id
    listing.status = Listing.STATUS_ARCHIVING
    if not _save_local_listing_intent(
        listing,
        update_fields=('status',),
        expected_status=expected_status,
        expected_external_id=expected_external_id,
    ):
        return {'status': 'stale'}
    _write_log(
        listing.tenant, 'listing_unpublish', 'ok',
        f'«{listing.title or listing.product.name}» будет снято с публикации ближайшим фидом Avito',
        listing=listing,
    )
    request_feed_flush(listing.account)


def _confirm_removal_dual_write(task, listing_id: int):
    claim, skip_reason = _claim_listing_status_check(
        listing_id,
        eligible_statuses=(Listing.STATUS_ARCHIVING,),
        require_external_id=True,
    )
    if claim is None:
        if skip_reason == 'missing_external_id':
            try:
                listing = _get_listing(listing_id)
            except Listing.DoesNotExist:
                return {'status': 'stale', 'changed': False}
            if listing.status == Listing.STATUS_ARCHIVING and not listing.external_id:
                listing.status = Listing.STATUS_ARCHIVED
                _save_local_listing_intent(
                    listing,
                    update_fields=('status',),
                    expected_status=Listing.STATUS_ARCHIVING,
                    expected_external_id=None,
                )
        return {'status': 'skipped', 'reason': skip_reason}

    owned = _load_owned_claimed_listings([claim])
    if not owned:
        return {'status': 'stale', 'changed': False}
    _owned_claim, listing = owned[0]
    try:
        data = AvitoAdapter(listing.account).get_status(listing)
    except RateLimitError as exc:
        countdown = max(exc.retry_after, backoff(task.request.retries))
        _release_status_claim(
            claim,
            next_status_check_at=now() + datetime.timedelta(seconds=countdown),
        )
        raise task.retry(exc=exc, countdown=countdown)
    except ServerError as exc:
        countdown = backoff(task.request.retries)
        _release_status_claim(
            claim,
            next_status_check_at=now() + datetime.timedelta(seconds=countdown),
        )
        raise task.retry(exc=exc, countdown=countdown)

    raw_status = (data or {}).get('status', '')
    normalized = normalize_remote_status(
        raw_status,
        aliases=_AVITO_REMOTE_STATUS_ALIASES,
    )
    checked_at = now()
    terminal = normalized in {
        Listing.REMOTE_STATUS_REJECTED,
        Listing.REMOTE_STATUS_BLOCKED,
        Listing.REMOTE_STATUS_REMOVED,
        Listing.REMOTE_STATUS_ARCHIVED,
    }
    canonical_updates: dict[str, object] = {}
    next_check_at: datetime.datetime | None = (
        checked_at + _TRANSIENT_STATUS_RECHECK_DELAY
    )
    if terminal:
        canonical_updates = {
            'status': Listing.STATUS_ARCHIVED,
            'last_sync_at': checked_at,
        }
        next_check_at = None

    affected = _apply_claimed_status_result(
        claim,
        raw_remote_status=raw_status,
        checked_at=checked_at,
        next_status_check_at=next_check_at,
        canonical_updates=canonical_updates,
    )
    if affected != 1:
        return {'status': 'stale', 'changed': False}
    if terminal:
        _write_log(
            listing.tenant, 'listing_unpublish', 'ok',
            f'«{listing.title or listing.product.name}» снято с публикации (в архиве)',
            listing=listing,
        )
        return {'status': 'archived', 'changed': True}
    return {
        'status': (
            'active'
            if normalized == Listing.REMOTE_STATUS_ACTIVE
            else 'ignored'
        ),
        'changed': False,
        'provider_status': normalized,
    }


@shared_task(bind=True, max_retries=3, queue='avito_update')
def confirm_removal_task(self, listing_id: int):
    """
    Подтверждает снятие: переводит «Снимается» → «В архиве», когда Avito
    перестал показывать объявление активным (autoload обрабатывает пакетно).
    """
    if _status_lifecycle_dual_write_enabled():
        return _confirm_removal_dual_write(self, listing_id)

    listing = _get_listing(listing_id)
    if listing.status != Listing.STATUS_ARCHIVING:
        return
    if not listing.external_id:
        listing.status = Listing.STATUS_ARCHIVED
        listing.save(update_fields=['status'])
        return
    try:
        data = AvitoAdapter(listing.account).get_status(listing)
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))
    avito_status = (data or {}).get('status', '')
    if avito_status and avito_status != 'active':
        listing.status = Listing.STATUS_ARCHIVED
        listing.last_sync_at = now()
        listing.save(update_fields=['status', 'last_sync_at'])
        _write_log(
            listing.tenant, 'listing_unpublish', 'ok',
            f'«{listing.title or listing.product.name}» снято с публикации (в архиве)',
            listing=listing,
        )
    # Ещё active — оставляем «Снимается»; периодическая сверка дожмёт.


@shared_task(bind=True, max_retries=6, queue='avito_delete')
def delete_listing_task(self, listing_id: int):
    """Удаляет объявление через фид (статус Remove)."""
    listing = _get_listing(listing_id)
    if _status_lifecycle_dual_write_enabled():
        # The service normally persists DELETED before dispatch. A delayed
        # task from an older generation must not delete a republished row.
        if listing.status != Listing.STATUS_DELETED:
            return {'status': 'stale'}
    else:
        listing.status = Listing.STATUS_DELETED
        listing.save(update_fields=['status'])
    if not listing.external_id:
        return
    # Удаление = отсутствие в фиде; уйдёт ближайшим часовым окном.
    _write_log(
        listing.tenant, 'listing_delete', 'ok',
        f'«{listing.title or listing.product.name}» будет удалено ближайшим фидом Avito',
        listing=listing,
    )
    request_feed_flush(listing.account)


def _enqueue_feed_run_revision(
    run_id: uuid.UUID | str,
    revision: int,
    *,
    available_at: datetime.datetime,
):
    """Persist one immutable feed wake-up in the generic durable outbox."""

    generation_id = uuid.UUID(str(run_id))
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError('revision must be a non-negative integer.')
    return enqueue_durable_task(
        _DURABLE_FEED_TASK_NAME,
        args=[str(generation_id), revision],
        deduplication_key=f'feed-run:{generation_id}:rev:{revision}',
        available_at=available_at,
        max_run_attempts=25,
        execution_timeout_seconds=180,
    )


def _enqueue_feed_run_snapshot(snapshot: FeedRunSnapshot):
    if snapshot.state in MarketplaceFeedRun.ACTIVE_STATES:
        if snapshot.next_attempt_at is None:
            return None
        available_at = snapshot.next_attempt_at
    elif snapshot.state in {
        MarketplaceFeedRun.State.SUCCEEDED,
        MarketplaceFeedRun.State.FAILED,
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    }:
        # The same allowlisted leaf delivers the deterministic terminal digest.
        available_at = now()
    else:
        return None
    return _enqueue_feed_run_revision(
        snapshot.run_id,
        snapshot.revision,
        available_at=available_at,
    )


def _feed_payload_bytes(feed_listings: list[Listing]) -> bytes:
    """Build the exact immutable byte sequence whose digest owns a run."""

    return build_feed(feed_listings) if feed_listings else build_stop_feed()


def _mark_feed_submission_boundary(
    claim: FeedRunClaim,
    *,
    provider_predecessor_run_id: str = '',
    submitted_at: datetime.datetime,
) -> FeedRunClaim:
    """Persist the pre-POST boundary while retaining this exact lease.

    SUBMIT_UNKNOWN is intentionally entered *before* the non-idempotent POST.
    A worker crash can then never leave a PREPARING row whose provider outcome
    is ambiguous. Revision fencing still advances even though ownership stays
    with the current worker.
    """

    boundary = persist_feed_submission_boundary(
        claim,
        provider_predecessor_run_id=provider_predecessor_run_id,
        submitted_at=submitted_at,
        now=now(),
    )
    if boundary is None:
        raise StaleFeedRunClaim('Feed submission boundary claim is stale.')
    return boundary


def _clear_rejected_feed_submission_boundary(
    claim: FeedRunClaim,
) -> FeedRunClaim:
    """Return a provider-rejected attempt to safe PREPARING state."""

    transition_at = now()
    changed = MarketplaceFeedRun.objects.filter(
        pk=claim.run_id,
        account_id=claim.account_id,
        tenant_id=claim.tenant_id,
        marketplace=claim.marketplace,
        account_identity_digest=claim.account_identity_digest,
        state=MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=transition_at,
        submitted_at=claim.submitted_at,
    ).update(
        state=MarketplaceFeedRun.State.PREPARING,
        submitted_at=None,
        provider_predecessor_run_id=None,
        provider_result_deadline_at=None,
        submission_reconcile_attempt=0,
        revision=F('revision') + 1,
        last_error='',
        updated_at=transition_at,
    )
    if changed != 1:
        raise StaleFeedRunClaim('Rejected feed submission claim is stale.')
    return replace(
        claim,
        state=MarketplaceFeedRun.State.PREPARING,
        revision=claim.revision + 1,
        submitted_at=None,
        provider_predecessor_run_id=None,
        provider_result_deadline_at=None,
    )


def _reschedule_coalesced_flush_after_conflict(account_id: int) -> None:
    """Keep one deferred flush while an older generation owns the account."""

    countdown = int(_DURABLE_FEED_SUBMISSION_DELAY.total_seconds())
    marker = f'avito:flush_scheduled:{account_id}'
    if cache.add(marker, 1, timeout=countdown + 60):
        coalesced_flush_task.apply_async(args=[account_id], countdown=countdown)


def _record_feed_run_summary(snapshot: FeedRunSnapshot, *, error: str = '') -> None:
    """Write one account-level audit row for a terminal feed generation."""

    from apps.sync.models import SyncLog

    failed = snapshot.state != MarketplaceFeedRun.State.SUCCEEDED
    status = SyncLog.STATUS_ERROR if failed else SyncLog.STATUS_OK
    message = (
        f'Фид {snapshot.marketplace} завершён: опубликовано '
        f'{snapshot.published_count}, отклонено {snapshot.rejected_count}, '
        f'прочих завершённых {snapshot.other_resolved_count}, '
        f'ожидает {snapshot.pending_count}.'
    )
    if error:
        message = f'{message} {_bounded_provider_reason(error)}'
    SyncLog.objects.create(
        tenant_id=snapshot.tenant_id,
        event_type=(
            SyncLog.EVENT_LISTING_ERROR if failed else SyncLog.EVENT_LISTING_PUBLISH
        ),
        status=status,
        message=message,
        payload={
            'feed_generation_id': str(snapshot.run_id),
            'marketplace': snapshot.marketplace,
            'state': snapshot.state,
            'total_count': snapshot.total_count,
            'published_count': snapshot.published_count,
            'rejected_count': snapshot.rejected_count,
            'other_resolved_count': snapshot.other_resolved_count,
            'pending_count': snapshot.pending_count,
        },
    )


def _send_feed_run_digest(run: MarketplaceFeedRun) -> None:
    """Synchronously execute the idempotent terminal notification leaf."""

    from apps.notifications.tasks import send_notification_task

    failed = run.state != MarketplaceFeedRun.State.SUCCEEDED
    message = (
        f'Обработка фида {run.marketplace} завершена: '
        f'опубликовано {run.published_count}, '
        f'отклонено {run.rejected_count}, '
        f'прочих завершённых '
        f'{run.total_count - run.published_count - run.rejected_count - run.pending_count}, '
        f'ожидает {run.pending_count}.'
    )
    if run.last_error:
        message = f'{message} {html.escape(_bounded_provider_reason(run.last_error))}'
    # This is deliberately synchronous inside BackgroundJobDispatch. Provider
    # delivery has its own deterministic event/channel idempotency rows, while
    # a retryable failure keeps this durable dispatch pending.
    send_notification_task.run(
        run.tenant_id,
        LEVEL_ERROR if failed else LEVEL_SUCCESS,
        message,
        payload={
            'feed_generation_id': str(run.pk),
            'state': run.state,
        },
        event_key=(
            f'marketplace-feed-run:{run.pk}:terminal:'
            f'{run.state}:rev:{run.revision}'
        ),
    )


def _finish_durable_feed_run(
    claim: FeedRunClaim,
    *,
    state: str,
    error: object = '',
    increment_submission_attempt: bool = False,
) -> FeedRunSnapshot:
    safe_error = _bounded_provider_reason(error)
    with transaction.atomic():
        snapshot = finish_feed_run(
            claim,
            state=state,
            error=safe_error,
            increment_submission_attempt=increment_submission_attempt,
            now=now(),
        )
        _record_feed_run_summary(snapshot, error=safe_error)
        _enqueue_feed_run_snapshot(snapshot)
    return snapshot


def _retry_durable_feed_step(
    claim: FeedRunClaim,
    error: object,
    *,
    delay: datetime.timedelta = _POLL_RETRY_DELAY,
    increment_report_attempt: bool = False,
    increment_submission_attempt: bool = False,
) -> FeedRunSnapshot:
    retry_at = now() + max(delay, datetime.timedelta(seconds=1))
    with transaction.atomic():
        snapshot = retry_step(
            claim,
            next_attempt_at=retry_at,
            error=_bounded_provider_reason(error),
            increment_report_attempt=increment_report_attempt,
            increment_submission_attempt=increment_submission_attempt,
            now=now(),
        )
        _enqueue_feed_run_snapshot(snapshot)
    return snapshot


def _record_durable_feed_payload_limit(
    account: MarketplaceAccount,
) -> dict[str, int | str]:
    """Record one bounded account-level error without per-listing fan-out."""

    result: dict[str, int | str] = {
        'status': 'payload_limit_exceeded',
        'limit': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS,
        'observed_at_least': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
    }
    _write_log(
        account.tenant,
        'listing_error',
        'error',
        (
            f'Фид Avito для аккаунта #{account.pk} не отправлен: '
            f'полный payload превышает лимит '
            f'{_MAX_DURABLE_FEED_PAYLOAD_LISTINGS} объявлений '
            f'(обнаружено не менее '
            f'{_MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1}).'
        ),
    )
    return result


def _provider_predecessor_from_strict_read(
    account: MarketplaceAccount,
    upload: object,
) -> str:
    """Return the authoritative latest upload id, or ``''`` for no upload."""

    if not isinstance(upload, dict):
        raise ValueError('Latest provider upload must be an object.')
    if not upload:
        return ''
    raw_run_id = upload.get('upload_id')
    if raw_run_id in (None, ''):
        raise ValueError('Latest provider upload has no upload_id.')
    provider_run_id = str(raw_run_id).strip()
    if not provider_run_id or len(provider_run_id) > 200:
        raise ValueError('Latest provider upload has an invalid upload_id.')
    expected_account_id = str(account.external_id)
    for field_name in ('account_id', 'user_id', 'owner_id'):
        observed_account_id = upload.get(field_name)
        if (
            observed_account_id not in (None, '')
            and str(observed_account_id) != expected_account_id
        ):
            raise ValueError('Latest provider upload belongs to another account.')
    return provider_run_id


def _retry_pre_submission_baseline(
    task,
    claim: FeedRunClaim,
    error: object,
) -> dict:
    """Release a proven pre-POST attempt; no ambiguous provider work exists."""

    retries = int(getattr(getattr(task, 'request', None), 'retries', 0))
    max_retries = int(getattr(task, 'max_retries', 0))
    if task is not None and retries >= max_retries:
        snapshot = _finish_durable_feed_run(
            claim,
            state=MarketplaceFeedRun.State.FAILED,
            error=error,
        )
        return {'status': 'failed_pre_submission', 'run_id': str(snapshot.run_id)}

    retry_delay = _provider_retry_delay(error)
    try:
        retry_step(
            claim,
            next_attempt_at=now() + retry_delay,
            error=error,
            now=now(),
        )
    except (FeedRunConflict, StaleFeedRunClaim, FeedWorkflowError):
        return {'status': 'stale_before_submission', 'run_id': str(claim.run_id)}
    if task is None:
        return {'status': 'pre_submission_retry', 'run_id': str(claim.run_id)}
    raise task.retry(
        exc=error if isinstance(error, Exception) else RuntimeError(str(error)),
        countdown=max(1, int(retry_delay.total_seconds())),
    )


def _coalesced_flush_durable(task, account: MarketplaceAccount):
    """Submit one exact feed generation without replaying ambiguous POSTs."""

    # Local writers use the same account-first lock order.  The byte snapshot
    # and generation tags therefore describe one coherent local intent.
    payload_limit_exceeded = False
    try:
        with transaction.atomic():
            locked_account = (
                MarketplaceAccount.objects.select_for_update(of=('self',))
                .select_related('tenant')
                .get(pk=account.pk, tenant__is_active=True)
            )
            feed_listings = _account_feed_listings(
                locked_account,
                limit=_MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
            )
            if len(feed_listings) > _MAX_DURABLE_FEED_PAYLOAD_LISTINGS:
                # The capped probe deliberately does not count the whole table:
                # the exact value is irrelevant once the accepted ceiling is
                # crossed, and an unbounded count would add avoidable DB work.
                payload_limit_exceeded = True
            else:
                payload = _feed_payload_bytes(feed_listings)
                payload_sha256 = hashlib.sha256(payload).hexdigest()
                run = create_or_supersede_feed_run(
                    locked_account.pk,
                    payload_sha256=payload_sha256,
                    now=now(),
                )
    except FeedSubmissionOutcomeUncertain:
        # An earlier POST may have succeeded. Retrying under another run UUID
        # is still a blind provider replay, so only manual reconciliation may
        # release this account-level hold.
        return {'status': 'manual_reconciliation_required'}
    except FeedRunConflict:
        # Never overwrite the shared provider feed object while a submitted or
        # claimed generation still owns it. Preserve one deferred flush marker
        # so local changes are sent after that generation resolves.
        _reschedule_coalesced_flush_after_conflict(account.pk)
        return {'status': 'active_feed_run'}
    except MarketplaceAccount.DoesNotExist:
        return {'status': 'inactive_account'}

    if payload_limit_exceeded:
        return _record_durable_feed_payload_limit(locked_account)

    claim = claim_due_run_for_account(
        locked_account.pk,
        expected_generation_id=run.run_id,
        expected_revision=run.revision,
        now=now(),
    )
    if claim is None:
        return {'status': 'stale', 'run_id': str(run.run_id)}

    adapter = AvitoAdapter(locked_account)
    try:
        # Uploading the immutable object is before the non-idempotent provider
        # boundary. A proven S3 failure may be retried by the flush owner.
        adapter._upload_to_s3(payload)
    except FeedUploadError as exc:
        if task.request.retries >= task.max_retries:
            snapshot = _finish_durable_feed_run(
                claim,
                state=MarketplaceFeedRun.State.FAILED,
                error=exc,
            )
            return {'status': 'failed', 'run_id': str(snapshot.run_id)}
        retry_delay = datetime.timedelta(seconds=backoff(task.request.retries))
        # Do not enqueue the generic step: PREPARING is exclusively owned by
        # this flush task. Its bounded Celery retry creates a fresh snapshot.
        retry_step(
            claim,
            next_attempt_at=now() + retry_delay,
            error=exc,
            now=now(),
        )
        raise task.retry(exc=exc, countdown=int(retry_delay.total_seconds()))

    try:
        if not validate_feed_submission_owner(claim, now=now()):
            return {'status': 'stale_before_submission', 'run_id': str(run.run_id)}
        predecessor_upload = adapter.get_latest_upload(strict=True)
        provider_predecessor_run_id = _provider_predecessor_from_strict_read(
            locked_account,
            predecessor_upload,
        )
    except (
        FeedUploadError,
        AvitoError,
        ServerError,
        RateLimitError,
        requests.RequestException,
        TrustedResponseError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        # The strict read is before the persisted boundary and before POST.
        # It is therefore safe to retry, but never safe to invent a baseline.
        return _retry_pre_submission_baseline(task, claim, exc)

    submitted_at = now()
    try:
        claim = _mark_feed_submission_boundary(
            claim,
            provider_predecessor_run_id=provider_predecessor_run_id,
            submitted_at=submitted_at,
        )
    except StaleFeedRunClaim:
        # The immutable object exists, but the account was revalidated under
        # lock and did not authorize the non-idempotent provider POST.
        return {'status': 'stale_before_submission', 'run_id': str(run.run_id)}
    try:
        adapter._trigger_autoload()
    except RateLimitError as exc:
        if task.request.retries >= task.max_retries:
            snapshot = _finish_durable_feed_run(
                claim,
                state=MarketplaceFeedRun.State.FAILED,
                error=exc,
            )
            return {'status': 'failed', 'run_id': str(snapshot.run_id)}
        claim = _clear_rejected_feed_submission_boundary(claim)
        retry_seconds = max(1, int(exc.retry_after))
        retry_step(
            claim,
            next_attempt_at=now() + datetime.timedelta(seconds=retry_seconds),
            error=exc,
            now=now(),
        )
        raise task.retry(exc=exc, countdown=retry_seconds)
    except (
        AmbiguousFeedSubmissionError,
        requests.RequestException,
        TrustedResponseError,
        ServerError,
    ) as exc:
        # The POST may have crossed the wire. Never submit it again blindly;
        # reconcile only against a provider run that started after this run.
        uncertain_at = now()
        with transaction.atomic():
            snapshot = mark_feed_submission_unknown(
                claim,
                submitted_at=submitted_at,
                next_attempt_at=uncertain_at + _DURABLE_FEED_SUBMISSION_DELAY,
                error=exc,
                now=uncertain_at,
            )
            MarketplaceAccount.objects.filter(pk=locked_account.pk).update(
                last_feed_flush_at=uncertain_at,
            )
            _enqueue_feed_run_snapshot(snapshot)
        return {'status': 'submission_unknown', 'run_id': str(snapshot.run_id)}
    except FeedUploadError as exc:
        # A concrete HTTP response proves this POST was rejected. Do not turn
        # a provider/account error into 10k per-listing notifications.
        snapshot = _finish_durable_feed_run(
            claim,
            state=MarketplaceFeedRun.State.FAILED,
            error=exc,
        )
        return {'status': 'failed', 'run_id': str(snapshot.run_id)}

    transition_at = now()
    with transaction.atomic():
        snapshot = mark_feed_submitted(
            claim,
            payload_sha256=payload_sha256,
            provider_run_id=None,
            submitted_at=submitted_at,
            next_attempt_at=transition_at + _DURABLE_FEED_SUBMISSION_DELAY,
            now=transition_at,
        )
        MarketplaceAccount.objects.filter(pk=locked_account.pk).update(
            last_feed_flush_at=submitted_at,
        )
        _enqueue_feed_run_snapshot(snapshot)
    _write_log(
        locked_account.tenant,
        'feed_flush',
        'ok',
        (
            f'Фид загружен: {snapshot.total_count} новых объявлений для '
            f'{locked_account.name}, ожидаем Avito'
        ),
    )
    return {'status': 'submitted', 'run_id': str(snapshot.run_id)}


def _promote_queued_feed_rows(account: MarketplaceAccount) -> int:
    queryset = Listing.objects.filter(
        account=account,
        status=Listing.STATUS_QUEUED,
        external_id__isnull=True,
    )
    if not _status_lifecycle_dual_write_enabled():
        return queryset.update(status=Listing.STATUS_PENDING)

    lifecycle = release_status_check(
        next_status_check_at=None,
    ).as_update_kwargs()
    with transaction.atomic():
        locked_account = (
            MarketplaceAccount.objects.select_for_update(of=('self',))
            .filter(pk=account.pk, is_active=True, tenant__is_active=True)
            .first()
        )
        if locked_account is None:
            return 0
        return queryset.update(status=Listing.STATUS_PENDING, **lifecycle)


@shared_task(bind=True, max_retries=5, queue='avito_publish')
def coalesced_flush_task(self, account_id: int):
    """
    Единый flush аккаунта по часовому окну Avito Autoload.

    Собирает АКТУАЛЬНОЕ состояние аккаунта (последнее решение тенанта по каждому
    объявлению) и загружает один фид. Все промежуточные действия между окнами
    только меняли статус — здесь они коалесятся (publish→archive за час → в фид
    объявление не попадёт). Запускается координатором request_feed_flush.
    """
    from apps.marketplaces.models import MarketplaceAccount
    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(pk=account_id)
    except MarketplaceAccount.DoesNotExist:
        return

    cache.delete(f'avito:flush_scheduled:{account_id}')

    # Окно ещё закрыто (напр. вызвали раньше времени) — перепланируем на открытие.
    if _feed_window_remaining(account) > 0:
        request_feed_flush(account)
        return

    # Промотируем «в очереди» → «на модерации»: они входят в этот фид.
    _promote_queued_feed_rows(account)

    pending_queryset = Listing.objects.filter(
        account=account,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    ).select_related('tenant', 'product')

    if _durable_feed_run_enabled():
        has_pending = pending_queryset.exists()
        # The durable owner materializes the complete payload exactly once,
        # under the account lock. These probes only decide feed versus STOP.
        has_feed_listings = has_pending or Listing.objects.filter(
            account=account,
            status__in=[
                Listing.STATUS_ACTIVE,
                Listing.STATUS_PENDING,
                Listing.STATUS_QUEUED,
            ],
        ).exists()
        has_removals = (
            not has_feed_listings
            and Listing.objects.filter(
                account=account,
                external_id__isnull=False,
                status__in=[Listing.STATUS_ARCHIVING, Listing.STATUS_DELETED],
            ).exists()
        )
        if not has_feed_listings and not has_removals:
            return

        if has_pending and not AvitoAdapter(account).is_autoload_active():
            limit_probe = _account_feed_listings(
                account,
                limit=_MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
            )
            if len(limit_probe) > _MAX_DURABLE_FEED_PAYLOAD_LISTINGS:
                return _record_durable_feed_payload_limit(account)
            for listing in pending_queryset:
                _reject_listing(
                    listing,
                    'Автозагрузка Avito не подключена или профиль Autoload '
                    'недоступен. Подключите Автозагрузку в настройках Avito '
                    'и повторите публикацию.',
                )
            return

        return _coalesced_flush_durable(self, account)

    pending = list(pending_queryset)
    feed_listings = _account_feed_listings(account)
    # Есть что снять с публикации? (последнее активное ушло в архив/удаление —
    # нужно отправить уменьшенный фид или STOP, даже если новых публикаций нет.)
    has_removals = Listing.objects.filter(
        account=account, external_id__isnull=False,
        status__in=[Listing.STATUS_ARCHIVING, Listing.STATUS_DELETED],
    ).exists()
    if not pending and not feed_listings and not has_removals:
        return

    if pending and not AvitoAdapter(account).is_autoload_active():
        for listing in pending:
            _reject_listing(
                listing,
                'Автозагрузка Avito не подключена или профиль Autoload недоступен. '
                'Подключите Автозагрузку в настройках Avito и повторите публикацию.',
            )
        return

    try:
        _flush_account_or_stop(account)
        account.last_feed_flush_at = now()
        account.save(update_fields=['last_feed_flush_at'])
        _write_log(
            account.tenant, 'feed_flush', 'ok',
            f'Фид загружен: {len(pending)} новых объявлений для {account.name}, ожидаем Avito',
        )
        poll_feed_results_task.apply_async(args=[account_id], countdown=300)
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=RATE_LIMIT_RETRY_COUNTDOWN)
    except (FeedUploadError, ServerError) as exc:
        if self.request.retries >= self.max_retries:
            reason = str(exc)
            for listing in pending:
                _reject_listing(listing, reason)
            return
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))


def _latest_upload_observation(
    account: MarketplaceAccount,
    claim: FeedRunClaim,
    upload: object,
) -> tuple[str, str] | None:
    """Validate one account-scoped, post-boundary provider observation."""

    if not isinstance(upload, dict):
        return None
    provider_run_id = str(upload.get('upload_id') or '').strip()
    if not provider_run_id or len(provider_run_id) > 200:
        return None
    started_at = parse_datetime(str(upload.get('started_at') or ''))
    # ``submitted_at`` is the closest durable boundary before the ambiguous
    # provider POST. PREPARING rows cannot bind observations at all. Avito can
    # report timestamps at second precision and its clock can lag ours, so a
    # bounded skew is allowed; anything older remains fail-closed.
    evidence_floor = claim.submitted_at
    if (
        started_at is None
        or started_at.utcoffset() is None
        or evidence_floor is None
        or started_at < evidence_floor - _DURABLE_FEED_UPLOAD_CLOCK_SKEW
    ):
        return None
    expected_account_id = str(account.external_id)
    for field_name in ('account_id', 'user_id', 'owner_id'):
        observed_account_id = upload.get(field_name)
        if (
            observed_account_id not in (None, '')
            and str(observed_account_id) != expected_account_id
        ):
            return None
    status = str(upload.get('status') or '').strip().casefold()
    if not status:
        return None
    return provider_run_id, status


def _latest_upload_evidence(
    account: MarketplaceAccount,
    claim: FeedRunClaim,
    upload: object,
) -> tuple[str, str] | None:
    """Validate temporal/account evidence; callers bind exact identity."""

    return _latest_upload_observation(account, claim, upload)


def _strict_latest_upload_observation(
    account: MarketplaceAccount,
    claim: FeedRunClaim,
) -> tuple[str, str] | None:
    upload = AvitoAdapter(account).get_latest_upload(strict=True)
    return _latest_upload_observation(account, claim, upload)


def _provider_retry_delay(error: object) -> datetime.timedelta:
    if isinstance(error, RateLimitError):
        return datetime.timedelta(seconds=max(1, int(error.retry_after)))
    return _POLL_RETRY_DELAY


def _record_submission_negative_or_fail(
    claim: FeedRunClaim,
    error: object,
) -> FeedRunSnapshot:
    """Count one authoritative negative read and eventually stop fail-closed.

    A successful provider response that contains no upload attributable to the
    exact pre-POST boundary is evidence only of absence at that instant.  It
    must never authorize another non-idempotent POST.  We therefore reconcile
    for a bounded horizon, then require an operator to resolve the uncertain
    outcome manually.
    """

    transition_at = now()
    negative_count = claim.submission_reconcile_attempt + 1
    horizon_reached = (
        claim.submitted_at is not None
        and transition_at >= (
            claim.submitted_at + _DURABLE_FEED_SUBMISSION_RECONCILE_HORIZON
        )
    )
    if (
        horizon_reached
        and negative_count >= _DURABLE_FEED_SUBMISSION_NEGATIVE_THRESHOLD
    ):
        return _finish_durable_feed_run(
            claim,
            state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
            error=(
                'outcome_uncertain: exact provider submission evidence was '
                'not found within the reconciliation horizon; manual '
                'reconciliation is required and automatic POST retry is disabled. '
                f'Last observation: {_bounded_provider_reason(error)}'
            ),
            increment_submission_attempt=True,
        )
    return _retry_durable_feed_step(
        claim,
        error,
        increment_submission_attempt=True,
    )


def _read_latest_upload_or_retry(
    claim: FeedRunClaim,
    account: MarketplaceAccount,
) -> tuple[str, str] | FeedRunSnapshot:
    try:
        upload = AvitoAdapter(account).get_latest_upload(strict=True)
    except (
        FeedUploadError,
        AvitoError,
        ServerError,
        RateLimitError,
        requests.RequestException,
        TrustedResponseError,
        TypeError,
        ValueError,
    ) as exc:
        return _retry_durable_feed_step(
            claim,
            exc,
            delay=_provider_retry_delay(exc),
            increment_report_attempt=(
                claim.state == MarketplaceFeedRun.State.REPORTING
            ),
        )
    evidence = _latest_upload_evidence(account, claim, upload)
    if evidence is None:
        if claim.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN:
            return _record_submission_negative_or_fail(
                claim,
                'Нет точного подтверждения запуска фида у провайдера.',
            )
        return _retry_durable_feed_step(
            claim,
            'Нет точного подтверждения запуска фида у провайдера.',
            increment_report_attempt=(
                claim.state == MarketplaceFeedRun.State.REPORTING
            ),
        )
    return evidence


def _process_submission_unknown(
    claim: FeedRunClaim,
    account: MarketplaceAccount,
) -> dict:
    evidence = _read_latest_upload_or_retry(claim, account)
    if isinstance(evidence, FeedRunSnapshot):
        return {
            'status': (
                'outcome_uncertain'
                if evidence.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
                else 'retry_wait'
            ),
            'run_id': str(claim.run_id),
        }
    provider_run_id, provider_status = evidence
    if claim.provider_predecessor_run_id is None:
        snapshot = _finish_durable_feed_run(
            claim,
            state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
            error=(
                'outcome_uncertain: ambiguous submission has no authoritative '
                'pre-POST provider baseline; manual reconciliation is required.'
            ),
        )
        return {'status': 'outcome_uncertain', 'run_id': str(snapshot.run_id)}
    if provider_run_id == claim.provider_predecessor_run_id:
        snapshot = _record_submission_negative_or_fail(
            claim,
            'Площадка всё ещё возвращает запуск, существовавший до POST.',
        )
        return {
            'status': (
                'outcome_uncertain'
                if snapshot.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN
                else 'retry_wait'
            ),
            'run_id': str(snapshot.run_id),
        }
    if provider_status not in {'processing', 'success', 'success_warning'}:
        _retry_durable_feed_step(
            claim,
            f'Неподтверждённый статус запуска фида: {provider_status}.',
        )
        return {'status': 'retry_wait', 'run_id': str(claim.run_id)}
    transition_at = now()
    with transaction.atomic():
        snapshot = record_provider_run_observation(
            claim,
            provider_run_id=provider_run_id,
            next_attempt_at=(
                transition_at
                + datetime.timedelta(seconds=_FEED_POLL_BATCH_DELAY_SECONDS)
            ),
            now=transition_at,
        )
        _enqueue_feed_run_snapshot(snapshot)
    return {
        'status': 'provider_run_bound',
        'run_id': str(snapshot.run_id),
        'provider_status': provider_status,
    }


_PROVIDER_SUCCESS_STATUSES = frozenset({'success', 'success_warning'})
_PROVIDER_READ_EXCEPTIONS = (
    FeedUploadError,
    AvitoError,
    ServerError,
    RateLimitError,
    requests.RequestException,
    TrustedResponseError,
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
)


def _provider_result_deadline_reached(claim: FeedRunClaim) -> bool:
    deadline = claim.provider_result_deadline_at
    return deadline is None or now() >= deadline


def _finish_provider_result_uncertain(
    claim: FeedRunClaim,
    reason: object,
) -> dict:
    snapshot = _finish_durable_feed_run(
        claim,
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        error=(
            'outcome_uncertain: provider result provenance could not be '
            f'completed automatically; manual reconciliation is required. {reason}'
        ),
    )
    return {'status': 'outcome_uncertain', 'run_id': str(snapshot.run_id)}


def _retry_provider_result_read(
    claim: FeedRunClaim,
    error: object,
) -> dict:
    _retry_durable_feed_step(
        claim,
        error,
        delay=_provider_retry_delay(error),
    )
    return {'status': 'retry_wait', 'run_id': str(claim.run_id)}


def _process_polling_feed_run(
    claim: FeedRunClaim,
    account: MarketplaceAccount,
) -> dict:
    if _provider_result_deadline_reached(claim):
        return _finish_provider_result_uncertain(
            claim,
            'The exact 48-hour provider-result deadline was reached.',
        )

    try:
        before = _strict_latest_upload_observation(account, claim)
    except _PROVIDER_READ_EXCEPTIONS as exc:
        return _retry_provider_result_read(claim, exc)
    if before is None:
        return _retry_provider_result_read(
            claim,
            'Нет доказуемого запуска фида после сохранённой границы отправки.',
        )
    provider_run_id, provider_status = before

    if not claim.provider_run_id:
        predecessor = claim.provider_predecessor_run_id
        if predecessor is None:
            return _finish_provider_result_uncertain(
                claim,
                'The submission has no authoritative pre-POST provider baseline.',
            )
        if provider_run_id == predecessor:
            return _retry_provider_result_read(
                claim,
                'Площадка всё ещё возвращает запуск, существовавший до POST.',
            )
        transition_at = now()
        with transaction.atomic():
            snapshot = record_provider_run_observation(
                claim,
                provider_run_id=provider_run_id,
                next_attempt_at=(
                    transition_at
                    + datetime.timedelta(seconds=_FEED_POLL_BATCH_DELAY_SECONDS)
                ),
                now=transition_at,
            )
            _enqueue_feed_run_snapshot(snapshot)
        return {
            'status': 'provider_run_bound',
            'run_id': str(snapshot.run_id),
            'provider_status': provider_status,
        }

    if provider_run_id != claim.provider_run_id:
        return _finish_provider_result_uncertain(
            claim,
            'The provider latest-upload identity moved away from the bound run.',
        )
    if provider_status == 'processing':
        return _retry_provider_result_read(
            claim,
            'Площадка продолжает обрабатывать точный запуск фида.',
        )
    if provider_status not in _PROVIDER_SUCCESS_STATUSES:
        return _retry_provider_result_read(
            claim,
            f'Запуск фида ещё не подтверждён: {provider_status}.',
        )

    # The provider report is the generation-wide rejection authority. It must
    # complete before any global ad-id endpoint can mutate local listings.
    if claim.report_completed_at is None:
        transition_at = now()
        with transaction.atomic():
            snapshot = start_reporting(
                claim,
                provider_run_id=provider_run_id,
                next_attempt_at=transition_at + _DURABLE_FEED_REPORT_DELAY,
                now=transition_at,
            )
            _enqueue_feed_run_snapshot(snapshot)
        return {'status': 'reporting', 'run_id': str(snapshot.run_id)}

    batch = load_poll_batch(claim, limit=_FEED_POLL_BATCH_SIZE, now=now())
    if not batch:
        transition_at = now()
        if claim.pending_count:
            with transaction.atomic():
                snapshot = reset_poll_round(
                    claim,
                    next_attempt_at=transition_at + _POLL_RETRY_DELAY,
                    now=transition_at,
                )
                _enqueue_feed_run_snapshot(snapshot)
            return {'status': 'poll_round_wait', 'run_id': str(snapshot.run_id)}
        snapshot = _finish_durable_feed_run(
            claim,
            state=MarketplaceFeedRun.State.SUCCEEDED,
        )
        return {'status': 'completed', 'run_id': str(snapshot.run_id)}

    local_ids = {get_ad_id(listing): listing.pk for listing in batch}
    try:
        results = AvitoAdapter(account).get_feed_results(list(local_ids))
    except _PROVIDER_READ_EXCEPTIONS as exc:
        return _retry_provider_result_read(claim, exc)

    try:
        after = _strict_latest_upload_observation(account, claim)
    except _PROVIDER_READ_EXCEPTIONS as exc:
        return _retry_provider_result_read(claim, exc)
    if after != before:
        return _finish_provider_result_uncertain(
            claim,
            'The exact upload changed while a provider ID page was being read.',
        )
    if _provider_result_deadline_reached(claim):
        return _finish_provider_result_uncertain(
            claim,
            'The exact 48-hour deadline was reached before the ID page commit.',
        )

    resolved: dict[int, str] = {}
    if isinstance(results, list):
        for item in results[:_FEED_POLL_BATCH_SIZE]:
            if not isinstance(item, dict):
                continue
            listing_id = local_ids.get(str(item.get('ad_id') or ''))
            external_id = str(item.get('avito_id') or '').strip()
            if listing_id is not None and external_id:
                resolved[listing_id] = external_id

    transition_at = now()
    with transaction.atomic():
        applied = apply_poll_page(
            claim,
            batch_listing_ids=[listing.pk for listing in batch],
            resolved_external_ids=resolved,
            last_listing_id=batch[-1].pk,
            next_attempt_at=(
                transition_at
                + datetime.timedelta(seconds=_FEED_POLL_BATCH_DELAY_SECONDS)
            ),
            occurred_at=transition_at,
        )
        _enqueue_feed_run_snapshot(applied.snapshot)
    return {
        'status': 'poll_page_applied',
        'run_id': str(claim.run_id),
        'page_size': len(batch),
        'published': applied.published_count,
    }


def _retry_reporting_or_fail(claim: FeedRunClaim, error: object) -> dict:
    if claim.report_attempt >= 24:
        return _finish_provider_result_uncertain(
            claim,
            (
                'The exact report exceeded its bounded retry budget. '
                f'Last error: {_bounded_provider_reason(error)}'
            ),
        )
    _retry_durable_feed_step(
        claim,
        error,
        increment_report_attempt=True,
    )
    return {'status': 'retry_wait', 'run_id': str(claim.run_id)}


def _process_reporting_feed_run(
    claim: FeedRunClaim,
    account: MarketplaceAccount,
) -> dict:
    if _provider_result_deadline_reached(claim):
        return _finish_provider_result_uncertain(
            claim,
            'The exact 48-hour provider-result deadline was reached during reporting.',
        )
    max_pages = min(100, max(1, int(settings.AVITO_API_MAX_PAGES)))
    if claim.report_page > max_pages:
        return _finish_provider_result_uncertain(
            claim,
            'The provider report exceeds the bounded 10,000-listing page limit.',
        )

    try:
        before = _strict_latest_upload_observation(account, claim)
    except _PROVIDER_READ_EXCEPTIONS as exc:
        return _retry_reporting_or_fail(claim, exc)
    if before is None:
        return _retry_reporting_or_fail(
            claim,
            'Нельзя доказать, что отчёт относится к текущему поколению фида.',
        )
    provider_run_id, provider_status = before
    if provider_run_id != claim.provider_run_id:
        return _finish_provider_result_uncertain(
            claim,
            'The provider latest-upload identity moved away from the reporting run.',
        )
    if provider_status not in _PROVIDER_SUCCESS_STATUSES:
        return _retry_reporting_or_fail(
            claim,
            f'Точный запуск фида ещё не завершён: {provider_status}.',
        )

    try:
        page = AvitoAdapter(account).get_feed_item_error_page(claim.report_page)
    except _PROVIDER_READ_EXCEPTIONS as exc:
        return _retry_reporting_or_fail(claim, exc)
    if page.next_page is not None and page.next_page > max_pages:
        return _finish_provider_result_uncertain(
            claim,
            'The provider report exceeds the bounded 10,000-listing page limit.',
        )

    try:
        after = _strict_latest_upload_observation(account, claim)
    except _PROVIDER_READ_EXCEPTIONS as exc:
        return _retry_reporting_or_fail(claim, exc)
    if after != before:
        return _finish_provider_result_uncertain(
            claim,
            'The exact upload changed while a provider report page was being read.',
        )
    if _provider_result_deadline_reached(claim):
        return _finish_provider_result_uncertain(
            claim,
            'The exact 48-hour deadline was reached before the report page commit.',
        )

    transition_at = now()
    with transaction.atomic():
        applied = apply_report_page(
            claim,
            current_page=claim.report_page,
            errors_by_ad_id=page.errors,
            next_page=page.next_page,
            next_attempt_at=(
                transition_at + _DURABLE_FEED_REPORT_DELAY
                if page.next_page is not None
                else None
            ),
            occurred_at=transition_at,
        )
        if applied.snapshot.state in MarketplaceFeedRun.TERMINAL_STATES:
            _record_feed_run_summary(applied.snapshot)
        _enqueue_feed_run_snapshot(applied.snapshot)
    return {
        'status': (
            'completed'
            if applied.snapshot.state == MarketplaceFeedRun.State.SUCCEEDED
            else (
                'report_completed'
                if applied.snapshot.report_completed_at is not None
                else 'report_page_applied'
            )
        ),
        'run_id': str(applied.snapshot.run_id),
        'rejected': applied.rejected_count,
    }


def _enqueue_current_feed_run(account_id: int) -> dict:
    current_time = now()
    run = (
        MarketplaceFeedRun.objects.filter(
            account_id=account_id,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            state__in=MarketplaceFeedRun.ACTIVE_STATES,
            next_attempt_at__isnull=False,
            next_attempt_at__lte=current_time,
            account__deleted_at__isnull=True,
            account__is_active=True,
            account__tenant__is_active=True,
            tenant__is_active=True,
        )
        .filter(
            Q(claim_token__isnull=True)
            | Q(claimed_until__isnull=True)
            | Q(claimed_until__lte=current_time),
        )
        .only('pk', 'revision', 'next_attempt_at')
        .first()
    )
    if run is None:
        return {'status': 'not_due'}
    dispatch = _enqueue_feed_run_revision(
        run.pk,
        run.revision,
        available_at=cast(datetime.datetime, run.next_attempt_at),
    )
    return {'status': 'enqueued', 'dispatch_id': str(dispatch.pk)}


@shared_task(
    name=_DURABLE_FEED_TASK_NAME,
    queue='avito_publish',
)
def process_marketplace_feed_run_step(run_id: str, revision: int, /):
    """Execute one exact durable feed revision; stale deliveries are no-ops."""

    try:
        generation_id = uuid.UUID(str(run_id))
        if str(generation_id) != str(run_id):
            raise ValueError('run_id must be canonical UUID text.')
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError('revision must be a non-negative integer.')
        expected_revision = revision
    except (TypeError, ValueError, OverflowError):
        return {'status': 'invalid'}
    if not _durable_feed_run_enabled():
        return {'status': 'disabled'}

    run = (
        MarketplaceFeedRun.objects.select_related('account', 'tenant')
        .filter(pk=generation_id)
        .first()
    )
    if run is None or run.revision != expected_revision:
        return {'status': 'stale'}
    if run.state in {
        MarketplaceFeedRun.State.SUCCEEDED,
        MarketplaceFeedRun.State.FAILED,
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    }:
        _send_feed_run_digest(run)
        return {'status': 'digest_delivered', 'run_id': str(run.pk)}

    claim = claim_due_run_for_account(
        run.account_id,
        expected_generation_id=generation_id,
        expected_revision=expected_revision,
        now=now(),
    )
    if claim is None:
        return {'status': 'stale'}
    account = (
        MarketplaceAccount.objects.select_related('tenant')
        .filter(pk=claim.account_id, tenant__is_active=True)
        .first()
    )
    if account is None:
        return {'status': 'stale'}

    try:
        if claim.state == MarketplaceFeedRun.State.PREPARING:
            # PREPARING is strictly before the persisted provider boundary.
            # Recovery has proof no POST was attempted and must never invent
            # an ambiguous timestamp or submit outside the flush owner.
            snapshot = _finish_durable_feed_run(
                claim,
                state=MarketplaceFeedRun.State.FAILED,
                error='Flush worker was lost before the provider submission boundary.',
            )
            return {'status': 'failed_pre_submission', 'run_id': str(snapshot.run_id)}
        if claim.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN:
            return _process_submission_unknown(claim, account)
        if claim.state == MarketplaceFeedRun.State.POLLING:
            return _process_polling_feed_run(claim, account)
        if claim.state == MarketplaceFeedRun.State.REPORTING:
            return _process_reporting_feed_run(claim, account)
        snapshot = _finish_durable_feed_run(
            claim,
            state=MarketplaceFeedRun.State.FAILED,
            error=f'Unsupported durable feed state: {claim.state}.',
        )
        return {'status': 'failed', 'run_id': str(snapshot.run_id)}
    except (FeedRunConflict, StaleFeedRunClaim, FeedWorkflowError):
        return {'status': 'stale'}


@shared_task(
    name='apps.marketplaces.tasks.dispatch_due_marketplace_feed_runs',
    queue='avito_update',
)
def dispatch_due_marketplace_feed_runs():
    """Recover at most 100 due feed runs without taking their domain leases."""

    if not _durable_feed_run_enabled():
        return {'selected': 0, 'enqueued': 0, 'cancelled': 0, 'status': 'disabled'}
    current_time = now()
    cancelled = cancel_feed_runs_for_inactive_owners(
        limit=_DURABLE_FEED_RECOVERY_BATCH_SIZE,
        now=current_time,
    )
    rows = list(
        MarketplaceFeedRun.objects.filter(
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            state__in=MarketplaceFeedRun.ACTIVE_STATES,
            next_attempt_at__isnull=False,
            next_attempt_at__lte=current_time,
            account__deleted_at__isnull=True,
            account__is_active=True,
            account__tenant__is_active=True,
            tenant__is_active=True,
        )
        .filter(
            Q(claim_token__isnull=True)
            | Q(claimed_until__isnull=True)
            | Q(claimed_until__lte=current_time),
        )
        .order_by('next_attempt_at', 'pk')
        .values_list('pk', 'revision', 'next_attempt_at')
        [:_DURABLE_FEED_RECOVERY_BATCH_SIZE]
    )
    dispatch_ids = []
    for run_id, revision, available_at in rows:
        dispatch = _enqueue_feed_run_revision(
            run_id,
            revision,
            available_at=cast(datetime.datetime, available_at),
        )
        dispatch_ids.append(dispatch.pk)
    return {
        'selected': len(rows),
        'enqueued': len(dispatch_ids),
        'cancelled': len(cancelled),
        'dispatch_ids': [str(dispatch_id) for dispatch_id in dispatch_ids],
    }


def _feed_errors_are_current(account) -> bool:
    """Актуальны ли ошибки last_successful-загрузки Avito для текущей попытки публикации.

    False — если последняя загрузка ещё обрабатывается или началась раньше нашего
    последнего flush (то есть отчёт относится к предыдущему фиду). Без данных для
    сравнения тоже False: лучше подождать ретрая, чем отклонить по старому отчёту.
    """
    upload = AvitoAdapter(account).get_latest_upload()
    if not upload or upload.get('status') == 'processing':
        return False
    started_at = parse_datetime(str(upload.get('started_at') or ''))
    if started_at is None:
        return False
    flushed_at = account.last_feed_flush_at
    if flushed_at is None:
        return True
    return started_at >= flushed_at - datetime.timedelta(minutes=5)


def _reject_claimed_feed_listing(
    claim: _ListingStatusClaim,
    listing: Listing,
    reason: object,
) -> int:
    """Apply an Autoload rejection only to the still-owned local intent."""

    safe_reason = _bounded_provider_reason(reason) or 'Avito отклонил объявление.'
    checked_at = now()
    values = release_status_check(next_status_check_at=None).as_update_kwargs()
    values.update({
        'status': Listing.STATUS_REJECTED,
        'rejection_reason': safe_reason,
        'last_sync_at': checked_at,
    })
    affected = _apply_claimed_listing_values(
        claim,
        values=values,
        next_status_check_at=None,
        nudge_status_due=False,
    )
    if affected == 1:
        _notify_error(listing.tenant, safe_reason, listing=listing)
    return affected


def _publish_claimed_feed_listing(
    claim: _ListingStatusClaim,
    listing: Listing,
    avito_id: object,
) -> int:
    checked_at = now()
    external_id = str(avito_id)
    next_check_at = checked_at + _TRANSIENT_STATUS_RECHECK_DELAY
    values = clear_remote_observation().as_update_kwargs()
    values.update(release_status_check(
        next_status_check_at=next_check_at,
    ).as_update_kwargs())
    values.update({
        'external_id': external_id,
        'status': Listing.STATUS_ACTIVE,
        'published_at': listing.published_at or checked_at,
        'rejection_reason': '',
    })
    affected = _apply_claimed_listing_values(
        claim,
        values=values,
        next_status_check_at=next_check_at,
        nudge_status_due=True,
    )
    if affected != 1:
        return 0
    url = listing.external_url or f'https://www.avito.ru/{external_id}'
    _notify_success(
        listing.tenant,
        (
            f'Объявление «{listing.title or listing.product.name}» опубликовано на Avito. '
            f'Аккаунт: {listing.account.name}. Ссылка: {url}'
        ),
        listing=listing,
    )
    return 1


def _schedule_feed_poll(account_id: int, *, countdown: int) -> None:
    poll_feed_results_task.apply_async(
        args=[account_id],
        countdown=max(1, int(countdown)),
    )


def _has_ready_pending_feed_rows(account_id: int) -> bool:
    check_time = now()
    return Listing.objects.filter(
        account_id=account_id,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    ).filter(
        Q(next_status_check_at__isnull=True)
        | Q(next_status_check_at__lte=check_time),
    ).filter(
        Q(status_check_claim_token__isnull=True)
        | Q(status_check_claimed_until__isnull=True)
        | Q(status_check_claimed_until__lte=check_time),
    ).exists()


def _poll_feed_results_dual_write(task, account_id: int):
    lock_identity = f'avito:feed-poll:{account_id}'
    with try_session_advisory_lock(lock_identity) as acquired:
        if not acquired:
            return {'status': 'locked'}
        return _poll_feed_results_dual_write_owned(task, account_id)


def _poll_feed_results_dual_write_owned(task, account_id: int):
    claims = _claim_pending_feed_rows(account_id)
    owned = _load_owned_claimed_listings(claims)
    if not owned:
        return

    account = owned[0][1].account
    owned_ids = {claim.listing_id for claim, _listing in owned}
    unowned = [claim for claim in claims if claim.listing_id not in owned_ids]
    if unowned:
        _release_status_claims(
            unowned,
            next_status_check_at=now() + _TRANSIENT_STATUS_RECHECK_DELAY,
            nudge_account=False,
        )

    ad_ids = [get_ad_id(listing) for _claim, listing in owned]
    try:
        results = AvitoAdapter(account).get_feed_results(ad_ids)
    except FeedUploadError as exc:
        rejected_count = sum(
            _reject_claimed_feed_listing(claim, listing, exc)
            for claim, listing in owned
        )
        if rejected_count:
            _write_log(
                account.tenant, 'feed_poll', 'error',
                (
                    f'Ошибка проверки Autoload для {len(owned)} объявлений '
                    f'{account.name}: {_bounded_provider_reason(exc)}'
                ),
            )
        return {
            'status': 'processed',
            'total': len(owned),
            'published': 0,
            'rejected': rejected_count,
            'pending': 0,
        }
    except (ServerError, RateLimitError) as exc:
        countdown = (
            max(exc.retry_after, int(_POLL_RETRY_DELAY.total_seconds()))
            if isinstance(exc, RateLimitError)
            else int(_POLL_RETRY_DELAY.total_seconds())
        )
        _release_status_claims(
            [claim for claim, _listing in owned],
            next_status_check_at=now() + datetime.timedelta(seconds=countdown),
            nudge_account=False,
        )
        _schedule_feed_poll(account_id, countdown=countdown)
        return {'status': 'rescheduled', 'reason': type(exc).__name__}

    mapping = {item['ad_id']: item.get('avito_id') for item in results}
    published_count = 0
    unresolved: list[tuple[_ListingStatusClaim, Listing]] = []
    for claim, listing in owned:
        avito_id = mapping.get(get_ad_id(listing))
        if avito_id:
            published_count += _publish_claimed_feed_listing(
                claim,
                listing,
                avito_id,
            )
        else:
            unresolved.append((claim, listing))

    if published_count:
        _write_log(
            account.tenant, 'feed_poll', 'ok',
            f'Получены ID Avito: {published_count}/{len(owned)} объявлений для {account.name}',
        )

    if unresolved:
        _release_status_claims(
            [claim for claim, _listing in unresolved],
            next_status_check_at=now() + _POLL_RETRY_DELAY,
            nudge_account=False,
        )

    if _has_ready_pending_feed_rows(account_id):
        _schedule_feed_poll(
            account_id,
            countdown=_FEED_POLL_BATCH_DELAY_SECONDS,
        )
    elif unresolved:
        _schedule_feed_poll(
            account_id,
            countdown=int(_POLL_RETRY_DELAY.total_seconds()),
        )

    return {
        'status': 'processed',
        'total': len(owned),
        'published': published_count,
        'rejected': 0,
        'pending': len(unresolved),
    }


@shared_task(bind=True, max_retries=10, queue='avito_publish')
def poll_feed_results_task(self, account_id: int):
    """
    Опрашивает Avito Autoload о результатах обработки фида.

    Сопоставляет ad_id (publish_idempotency_key) с avito_id и обновляет
    Listing.external_id + status='active' для опубликованных объявлений.

    Запускается через 5 мин после coalesced_flush_task; при необходимости повторяет.
    """
    if _durable_feed_run_enabled():
        return _enqueue_current_feed_run(account_id)
    if _status_lifecycle_dual_write_enabled():
        return _poll_feed_results_dual_write(self, account_id)

    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(pk=account_id)
    except MarketplaceAccount.DoesNotExist:
        return

    pending = list(
        Listing.objects.filter(
            account=account,
            status=Listing.STATUS_PENDING,
            external_id__isnull=True,
        ).select_related('tenant', 'product')
    )
    if not pending:
        return

    ad_ids = [get_ad_id(lst) for lst in pending]
    try:
        results = AvitoAdapter(account).get_feed_results(ad_ids)
    except FeedUploadError as exc:
        reason = str(exc)
        for listing in pending:
            _reject_listing(listing, reason)
        _write_log(
            account.tenant, 'feed_poll', 'error',
            f'Ошибка проверки Autoload для {len(pending)} объявлений {account.name}: {reason}',
        )
        return
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))

    # Индекс: ad_id → avito_id
    mapping = {item['ad_id']: item.get('avito_id') for item in results}

    published_count = 0
    unresolved = []
    for listing in pending:
        avito_id = mapping.get(get_ad_id(listing))
        if avito_id:
            listing.external_id = str(avito_id)
            listing.status = Listing.STATUS_ACTIVE
            listing.published_at = listing.published_at or now()
            listing.rejection_reason = ''
            listing.save(update_fields=['external_id', 'status', 'published_at', 'rejection_reason'])
            url = f'https://www.avito.ru/{avito_id}'
            if listing.external_url:
                url = listing.external_url
            _notify_success(
                listing.tenant,
                (
                    f'Объявление «{listing.title or listing.product.name}» опубликовано на Avito. '
                    f'Аккаунт: {listing.account.name}. Ссылка: {url}'
                ),
                listing=listing,
            )
            published_count += 1
        else:
            unresolved.append(listing)

    _write_log(
        account.tenant, 'feed_poll', 'ok',
        f'Получены ID Avito: {published_count}/{len(pending)} объявлений для {account.name}',
    )

    # Среди необработанных сразу отклоняем те, у кого Avito уже вернул
    # блокирующие ошибки (с реальным текстом) — не ждём 50 минут ретраев.
    # Но только если ошибки относятся к ТЕКУЩЕЙ загрузке: при перепубликации
    # ad_id не меняется, и пока свежий фид обрабатывается, last_successful —
    # это предыдущая загрузка; её старые ошибки отклоняли уже исправленные
    # объявления устаревшей причиной.
    item_errors = {}
    if unresolved and _feed_errors_are_current(account):
        try:
            raw_errors = AvitoAdapter(account).get_feed_item_errors(
                [get_ad_id(lst) for lst in unresolved]
            )
            item_errors = raw_errors if isinstance(raw_errors, dict) else {}
        except Exception:
            item_errors = {}

    truly_pending = []
    rejected_count = 0
    for listing in unresolved:
        item_error = item_errors.get(get_ad_id(listing))
        if item_error:
            _reject_listing(listing, item_error)
            rejected_count += 1
        else:
            truly_pending.append(listing)

    # Остались те, по кому Avito пока не дал ни ID, ни ошибок — ещё обрабатывается.
    if truly_pending and self.request.retries < self.max_retries:
        raise self.retry(
            exc=RuntimeError(f'{len(truly_pending)} listing(s) still pending'),
            countdown=300,
        )
    if truly_pending:
        # Ретраи исчерпаны. Если загрузка у Avito ВСЁ ЕЩЁ обрабатывается
        # (Autoload бывает медленным — часами), не отклоняем: оставляем PENDING,
        # периодическая сверка check_moderation_status дожмёт опрос позже.
        if AvitoAdapter(account).get_latest_upload().get('status') == 'processing':
            return
        generic_reason = (
            'Avito обработал фид, но не вернул ни ID объявления, ни ошибок. '
            'Проверьте статус позже или в личном кабинете Avito Autoload.'
        )
        for listing in truly_pending:
            _reject_listing(listing, generic_reason)

    if rejected_count or truly_pending:
        _write_log(
            account.tenant, 'feed_poll', 'error',
            f'Не опубликовано {rejected_count + len(truly_pending)}/{len(pending)} '
            f'объявлений {account.name}',
        )


def _check_moderation_dual_write(task, listing_id: int):
    claim, skip_reason = _claim_listing_status_check(
        listing_id,
        eligible_statuses=(
            Listing.STATUS_PENDING,
            Listing.STATUS_ACTIVE,
            Listing.STATUS_REJECTED,
        ),
        require_external_id=True,
    )
    if claim is None:
        return {'status': 'skipped', 'reason': skip_reason}

    owned = _load_owned_claimed_listings([claim])
    if not owned:
        return {'status': 'stale', 'changed': False}
    _owned_claim, listing = owned[0]
    try:
        data = AvitoAdapter(listing.account).get_status(listing)
    except RateLimitError as exc:
        countdown = max(exc.retry_after, backoff(task.request.retries))
        _release_status_claim(
            claim,
            next_status_check_at=now() + datetime.timedelta(seconds=countdown),
        )
        raise task.retry(exc=exc, countdown=countdown)
    except ServerError as exc:
        countdown = backoff(task.request.retries)
        _release_status_claim(
            claim,
            next_status_check_at=now() + datetime.timedelta(seconds=countdown),
        )
        raise task.retry(exc=exc, countdown=countdown)

    response = data if isinstance(data, dict) else {}
    raw_status = response.get('status', '')
    normalized = normalize_remote_status(
        raw_status,
        aliases=_AVITO_REMOTE_STATUS_ALIASES,
    )
    checked_at = now()
    canonical_updates: dict[str, object] = {}
    changed = False

    if normalized == Listing.REMOTE_STATUS_ACTIVE:
        result_status = 'active'
        next_check_at: datetime.datetime | None = (
            checked_at + _ACTIVE_STATUS_RECHECK_DELAY
        )
        changed = (
            listing.status != Listing.STATUS_ACTIVE
            or bool(listing.rejection_reason)
        )
        if changed:
            canonical_updates = {
                'status': Listing.STATUS_ACTIVE,
                'rejection_reason': '',
                'last_sync_at': checked_at,
            }
    elif normalized in {
        Listing.REMOTE_STATUS_REJECTED,
        Listing.REMOTE_STATUS_BLOCKED,
    }:
        result_status = 'rejected'
        next_check_at = None
        rejection_reason = _bounded_provider_reason(
            response.get('rejection_reason', ''),
        )
        changed = (
            listing.status != Listing.STATUS_REJECTED
            or listing.rejection_reason != rejection_reason
        )
        if changed:
            canonical_updates = {
                'status': Listing.STATUS_REJECTED,
                'rejection_reason': rejection_reason,
                'last_sync_at': checked_at,
            }
    elif normalized in {
        Listing.REMOTE_STATUS_REMOVED,
        Listing.REMOTE_STATUS_ARCHIVED,
    }:
        result_status = 'archived'
        next_check_at = None
        changed = listing.status != Listing.STATUS_ARCHIVED
        if changed:
            canonical_updates = {
                'status': Listing.STATUS_ARCHIVED,
                'last_sync_at': checked_at,
            }
    else:
        result_status = 'ignored'
        next_check_at = checked_at + _TRANSIENT_STATUS_RECHECK_DELAY

    affected = _apply_claimed_status_result(
        claim,
        raw_remote_status=raw_status,
        checked_at=checked_at,
        next_status_check_at=next_check_at,
        canonical_updates=canonical_updates,
    )
    if affected != 1:
        return {'status': 'stale', 'changed': False}

    if normalized == Listing.REMOTE_STATUS_ACTIVE:
        _queue_listing_expiry_notification(
            listing,
            response,
            checked_at=checked_at,
        )

    if changed and result_status == 'active':
        _write_log(
            listing.tenant, 'moderation', 'ok',
            (
                f'Объявление «{listing.title or listing.product.name}» '
                'прошло модерацию Avito'
            ),
            listing=listing,
        )
    elif changed and result_status == 'rejected':
        rejection_reason = str(canonical_updates.get('rejection_reason', ''))
        reason_txt = f': {rejection_reason}' if rejection_reason else ''
        _notify_error(
            listing.tenant,
            (
                f'Объявление «{listing.title or listing.product.name}» '
                'отклонено при модерации Avito'
                + reason_txt
            ),
            listing=listing,
        )
        _write_log(
            listing.tenant, 'moderation', 'warn',
            f'Отклонено модерацией{reason_txt}',
            listing=listing,
        )
    elif changed and result_status == 'archived':
        _write_log(
            listing.tenant, 'moderation', 'warn',
            (
                f'Объявление «{listing.title or listing.product.name}» '
                'больше не активно на Avito'
            ),
            listing=listing,
        )

    if result_status == 'ignored':
        return {
            'status': 'ignored',
            'provider_status': Listing.REMOTE_STATUS_OTHER,
        }
    return {'status': result_status, 'changed': changed}


@shared_task(bind=True, max_retries=3, queue='avito_update')
def check_moderation_task(self, listing_id: int):
    if _status_lifecycle_dual_write_enabled():
        return _check_moderation_dual_write(self, listing_id)

    listing = _get_listing(listing_id)
    if not listing.external_id:
        return {'status': 'skipped', 'reason': 'missing_external_id'}
    try:
        data = AvitoAdapter(listing.account).get_status(listing)
        avito_status = data.get('status', '')
        checked_at = now()
        changed = False
        if avito_status == 'active':
            changed = listing.status != Listing.STATUS_ACTIVE
            listing.status = Listing.STATUS_ACTIVE
            if changed:
                _write_log(
                    listing.tenant, 'moderation', 'ok',
                    (
                        f'Объявление «{listing.title or listing.product.name}» '
                        'прошло модерацию Avito'
                    ),
                    listing=listing,
                )
        elif avito_status in ('rejected', 'blocked'):
            rejection_reason = data.get('rejection_reason', '')
            changed = (
                listing.status != Listing.STATUS_REJECTED
                or listing.rejection_reason != rejection_reason
            )
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = rejection_reason
            if changed:
                reason_txt = (
                    f': {listing.rejection_reason}'
                    if listing.rejection_reason else ''
                )
                _notify_error(
                    listing.tenant,
                    (
                        f'Объявление «{listing.title or listing.product.name}» '
                        'отклонено при модерации Avito'
                        + reason_txt
                    ),
                    listing=listing,
                )
                _write_log(
                    listing.tenant, 'moderation', 'warn',
                    f'Отклонено модерацией{reason_txt}',
                    listing=listing,
                )
        listing.last_sync_at = checked_at
        listing.save(update_fields=['status', 'rejection_reason', 'last_sync_at'])
        if avito_status in ('rejected', 'blocked'):
            return {'status': 'rejected', 'changed': changed}
        if avito_status == 'active':
            _queue_listing_expiry_notification(
                listing,
                data,
                checked_at=checked_at,
            )
            return {'status': 'active', 'changed': changed}
        return {'status': 'ignored', 'provider_status': avito_status}
    except RateLimitError as exc:
        raise self.retry(
            exc=exc,
            countdown=max(exc.retry_after, backoff(self.request.retries)),
        )
    except ServerError as exc:
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))


@shared_task(queue='avito_update')
def check_moderation_status():
    """
    Запускает проверку статуса модерации и результатов Autoload.

    Запускается каждые 30 минут через Celery Beat.
    """
    pending_account_ids = list(Listing.objects.filter(
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    ).values_list('account_id', flat=True).distinct())
    for account_id in pending_account_ids:
        poll_feed_results_task.delay(account_id)

    queued_account_ids = list(Listing.objects.filter(
        status=Listing.STATUS_QUEUED,
        external_id__isnull=True,
    ).values_list('account_id', flat=True).distinct())
    for account_id in queued_account_ids:
        listing_id = (
            Listing.objects.filter(
                account_id=account_id,
                status=Listing.STATUS_QUEUED,
                external_id__isnull=True,
            ).order_by('created_at', 'pk').values_list('pk', flat=True).first()
        )
        if listing_id:
            publish_listing_task.delay(listing_id)

    active_listing_ids = list(Listing.objects.filter(
        status=Listing.STATUS_ACTIVE,
    ).values_list('pk', flat=True))

    for listing_id in active_listing_ids:
        check_moderation_task.delay(listing_id)

    # «Снимается» → подтверждаем снятие (Avito обрабатывает пакетно).
    archiving_ids = list(Listing.objects.filter(
        status=Listing.STATUS_ARCHIVING,
        external_id__isnull=False,
    ).values_list('pk', flat=True))
    for listing_id in archiving_ids:
        confirm_removal_task.delay(listing_id)

    return {
        'pending_accounts_queued': len(pending_account_ids),
        'queued_accounts_started': len(queued_account_ids),
        'active_listings_queued': len(active_listing_ids),
        'archiving_confirmed': len(archiving_ids),
    }


@shared_task(queue='avito_update')
def reconcile_listings():
    """
    Сверяет статусы листингов на Avito с данными в БД.

    Запускается ежедневно в 03:00 через Celery Beat.
    """
    listing_ids = list(Listing.objects.filter(
        status=Listing.STATUS_ACTIVE,
        external_id__isnull=False,
    ).values_list('pk', flat=True))

    for listing_id in listing_ids:
        check_moderation_task.delay(listing_id)

    return {'listings_reconciled': len(listing_ids)}


@shared_task(queue='avito_update')
def refresh_avito_stats():
    """
    Ежечасно обновляет ListingStats и запускает проверку теневого бана.

    За каждый аккаунт: запрашивает статистику за вчера и сегодня из Avito Stats API,
    сохраняет в ListingStats, параллельно проверяет shadow ban.
    """
    from apps.anti_ban.tasks import check_shadow_ban_task
    from apps.marketplaces.models import MarketplaceAccount

    today = datetime.date.today()
    date_from = today - datetime.timedelta(days=1)

    account_ids = list(MarketplaceAccount.objects.filter(
        is_active=True,
    ).values_list('pk', flat=True))

    for account_id in account_ids:
        check_shadow_ban_task.delay(account_id)
        fetch_stats_for_account_task.delay(account_id, str(date_from), str(today))

    return {'accounts_scheduled': len(account_ids)}


@shared_task(bind=True, max_retries=3, retry_backoff=True, queue='avito_update')
def fetch_stats_for_account_task(self, account_id: int, date_from_str: str, date_to_str: str):
    """
    Загружает статистику одного аккаунта Avito за указанный период.

    Вызывается из refresh_avito_stats и management command refresh_stats_history.
    """
    from apps.marketplaces.models import MarketplaceAccount
    from apps.marketplaces.services import StatsService

    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(pk=account_id)
    except MarketplaceAccount.DoesNotExist:
        return

    date_from = datetime.date.fromisoformat(date_from_str)
    date_to = datetime.date.fromisoformat(date_to_str)

    try:
        count = StatsService.fetch_for_account(account, date_from, date_to)
        return {'account_id': account_id, 'records': count}
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, queue='avito_publish')
def setup_autoload_profile_task(self, account_id: int, tenant_id: int):
    """
    Регистрирует feed URL тенанта в профиле Avito Autoload.

    Вызывается автоматически после создания MarketplaceAccount.
    Использует email владельца тенанта как report_email.
    При ошибке делает retry — не блокирует создание аккаунта.
    """
    from apps.marketplaces.models import MarketplaceAccount
    from apps.tenants.models import TenantUser

    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(
            pk=account_id,
            tenant_id=tenant_id,
        )
    except MarketplaceAccount.DoesNotExist:
        return

    owner = (
        TenantUser.objects
        .filter(tenant=account.tenant, role=TenantUser.ROLE_OWNER)
        .select_related('user')
        .first()
    )
    report_email = owner.user.email if owner else account.tenant.slug + '@map.local'

    try:
        AvitoAdapter(account).setup_autoload_profile(report_email)
        from apps.marketplaces.services import AvitoAccountStatusService
        AvitoAccountStatusService.refresh(account)
        _write_log(
            account.tenant, 'autoload_profile_setup', 'ok',
            f'Autoload профиль Avito настроен для {account.name}',
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))


@shared_task(queue='avito_update')
def refresh_avito_account_statuses():
    """Ставит проверку состояния для всех активных Avito-аккаунтов."""
    from apps.marketplaces.models import MarketplaceAccount
    from apps.tenants.models import Tenant

    queued = 0
    tenant_ids = Tenant.objects.filter(is_active=True).values_list('pk', flat=True)
    for tenant_id in tenant_ids.iterator():
        accounts = MarketplaceAccount.objects.filter(
            tenant_id=tenant_id,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            is_active=True,
        ).values_list('pk', flat=True)
        for account_id in accounts.iterator():
            refresh_avito_account_status_task.delay(account_id, tenant_id)
            queued += 1
    return {'queued': queued}


@shared_task(queue='avito_update')
def refresh_avito_account_status_task(account_id: int, tenant_id: int):
    """Идемпотентно обновляет один tenant-scoped снимок состояния Avito."""
    from apps.marketplaces.models import MarketplaceAccount
    from apps.marketplaces.services import AvitoAccountStatusService

    lock_key = f'avito:account-status:{tenant_id}:{account_id}'
    lock = _coordination_lock(lock_key, timeout=120)
    if not lock.acquire(blocking=False):
        return {'status': 'locked'}
    try:
        try:
            account = MarketplaceAccount.objects.select_related('tenant').get(
                pk=account_id,
                tenant_id=tenant_id,
                marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            )
        except MarketplaceAccount.DoesNotExist:
            return {'status': 'not_found'}
        status_obj = AvitoAccountStatusService.refresh(account)
        return {
            'status': 'ok',
            'autoload_status': status_obj.autoload_status,
            'tariff_status': status_obj.tariff_status,
        }
    finally:
        lock.release()


@shared_task(bind=True, max_retries=2, queue='avito_update')
def sync_avito_category_tree(self):
    """Еженедельно обновляет проверенный снимок дерева и мягко применяет его."""
    from apps.marketplaces.avito_tree_sync import AvitoCategoryTreeSyncService

    lock = _coordination_lock('avito:category-tree-sync:auto_parts', timeout=3300)
    if not lock.acquire(blocking=False):
        return {'status': 'locked'}
    try:
        try:
            return {
                'status': 'ok',
                **AvitoCategoryTreeSyncService.sync_auto_parts(),
            }
        except Exception as exc:
            raise self.retry(exc=exc, countdown=300 * (self.request.retries + 1))
    finally:
        lock.release()
