"""Truthful tenant-facing delivery state for marketplace listings.

``Listing.status`` is the business lifecycle state.  A pending listing can
still be local, can be inside an immutable provider submission, or can require
manual reconciliation.  Keep that distinction in one place so API labels and
write fences use the same evidence.
"""

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings

from apps.marketplaces.feed_cutover import private_feed_cutover_enabled
from apps.marketplaces.models import Listing, MarketplaceFeedRun


@dataclass(frozen=True, slots=True)
class ListingDeliveryPresentation:
    stage: str
    label: str
    provider_submission_started: bool
    lifecycle_actions_blocked: bool
    can_check_avito_status: bool
    retry_at: datetime | None = None
    retry_reason: str = ''


_PRIVATE_RETRY_REASONS = {
    'private_artifact_verification': (
        'MAP повторно проверяет сохранённую версию XML-фида.'
    ),
    'provider_baseline_read': (
        'Avito временно не вернул состояние предыдущей автозагрузки.'
    ),
    'private_promotion_boundary': (
        'MAP повторно проверяет, что URL и версия XML-фида не изменились.'
    ),
    'provider_rate_limit': (
        'Avito временно ограничил частоту запросов.'
    ),
}


def _retry_reason(last_error: str) -> str:
    reason_code = str(last_error or '').partition(':')[0].strip()
    return _PRIVATE_RETRY_REASONS.get(
        reason_code,
        'Временная техническая ошибка произошла до отправки фида в Avito.',
    )


def durable_feed_run_enabled(account_id: int) -> bool:
    """Return whether this account has exact durable generation evidence."""

    return (
        settings.MARKETPLACE_FEED_RUN_MODE == 'durable'
        and settings.AVITO_STATUS_LIFECYCLE_MODE == 'dual_write'
    ) or private_feed_cutover_enabled(account_id)


def _legacy_submission_outcome_unknown(listing: Listing) -> bool:
    """Return whether the account cursor proves an unresolved legacy POST.

    A normal legacy PENDING row kept the old editable lifecycle.  Only the
    explicit ``desired > dispatched`` plus ``due=NULL`` boundary means that a
    provider POST may have crossed the wire and cannot yet be followed safely.
    """

    account = listing.account
    return (
        account.feed_intent_revision
        > account.feed_intent_dispatched_revision
        and account.feed_intent_due_at is None
    )


def _provider_submission_started(run: MarketplaceFeedRun) -> bool:
    return (
        run.state
        in {
            MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
            MarketplaceFeedRun.State.POLLING,
            MarketplaceFeedRun.State.REPORTING,
            MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        }
        or run.submitted_at is not None
        or bool(run.provider_run_id)
        or bool(run.provider_predecessor_run_id)
    )


def _feed_run(listing: Listing) -> MarketplaceFeedRun | None:
    if listing.feed_run_id is None:
        return None
    return listing.feed_run


def listing_delivery_presentation(
    listing: Listing,
    *,
    run: MarketplaceFeedRun | None = None,
    durable_enabled: bool | None = None,
) -> ListingDeliveryPresentation:
    """Describe the exact delivery phase without changing DB status choices."""

    if listing.status == Listing.STATUS_QUEUED:
        return ListingDeliveryPresentation(
            stage='local_queue',
            label='В очереди на подготовку',
            provider_submission_started=False,
            lifecycle_actions_blocked=False,
            can_check_avito_status=False,
        )
    if listing.status != Listing.STATUS_PENDING:
        return ListingDeliveryPresentation(
            stage=listing.status,
            label=listing.get_status_display(),
            provider_submission_started=bool(listing.external_id),
            lifecycle_actions_blocked=False,
            can_check_avito_status=False,
        )

    if run is None:
        run = _feed_run(listing)
    if durable_enabled is None:
        durable_enabled = durable_feed_run_enabled(listing.account_id)

    if run is None:
        if durable_enabled:
            return ListingDeliveryPresentation(
                stage='awaiting_feed',
                label='Готовится к отправке в Avito',
                provider_submission_started=False,
                lifecycle_actions_blocked=False,
                can_check_avito_status=False,
            )
        # Preserve the old lifecycle behavior unless the account cursor proves
        # a genuinely ambiguous provider boundary.
        return ListingDeliveryPresentation(
            stage='legacy_delivery',
            label='Отправляется или обрабатывается Avito',
            provider_submission_started=True,
            lifecycle_actions_blocked=_legacy_submission_outcome_unknown(
                listing,
            ),
            can_check_avito_status=True,
        )

    state = run.state
    if state == MarketplaceFeedRun.State.PREPARING:
        if run.last_error and run.next_attempt_at is not None:
            return ListingDeliveryPresentation(
                stage='delivery_retry',
                label='Отправка временно задержана, повторяем',
                provider_submission_started=False,
                lifecycle_actions_blocked=False,
                can_check_avito_status=False,
                retry_at=run.next_attempt_at,
                retry_reason=_retry_reason(run.last_error),
            )
        return ListingDeliveryPresentation(
            stage='feed_preparing',
            label='Фид готовится к отправке',
            provider_submission_started=False,
            lifecycle_actions_blocked=False,
            can_check_avito_status=False,
        )
    if state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN:
        return ListingDeliveryPresentation(
            stage='submission_unknown',
            label='Проверяем, принял ли Avito фид',
            provider_submission_started=True,
            lifecycle_actions_blocked=True,
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.POLLING:
        return ListingDeliveryPresentation(
            stage='avito_processing',
            label='Avito обрабатывает фид',
            provider_submission_started=True,
            lifecycle_actions_blocked=False,
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.REPORTING:
        return ListingDeliveryPresentation(
            stage='avito_reporting',
            label='Получаем результат Avito',
            provider_submission_started=True,
            lifecycle_actions_blocked=False,
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.RETRY_WAIT:
        return ListingDeliveryPresentation(
            stage='delivery_retry',
            label='Отправка временно задержана, повторяем',
            provider_submission_started=_provider_submission_started(run),
            lifecycle_actions_blocked=False,
            can_check_avito_status=True,
            retry_at=run.next_attempt_at,
        )
    if state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN:
        return ListingDeliveryPresentation(
            stage='manual_review',
            label='Результат Avito требует ручной проверки',
            provider_submission_started=True,
            lifecycle_actions_blocked=True,
            can_check_avito_status=False,
        )
    if state == MarketplaceFeedRun.State.FAILED:
        return ListingDeliveryPresentation(
            stage='delivery_failed',
            label='Ошибка отправки в Avito',
            provider_submission_started=False,
            lifecycle_actions_blocked=False,
            can_check_avito_status=False,
        )
    if state == MarketplaceFeedRun.State.SUCCEEDED:
        return ListingDeliveryPresentation(
            stage='avito_processing',
            label='Ожидает завершения обработки Avito',
            provider_submission_started=True,
            lifecycle_actions_blocked=False,
            can_check_avito_status=True,
        )
    return ListingDeliveryPresentation(
        stage='delivery_stopped',
        label='Отправка остановлена, ожидается повтор',
        provider_submission_started=False,
        lifecycle_actions_blocked=False,
        can_check_avito_status=False,
    )
