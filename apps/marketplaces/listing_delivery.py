"""Truthful tenant-facing delivery state for marketplace listings.

``Listing.status`` is the business lifecycle state.  A pending listing can
still be local, can be inside an immutable provider submission, or can require
manual reconciliation.  Keep that distinction in one place so API labels and
write fences use the same evidence.
"""

from dataclasses import dataclass

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


def durable_feed_run_enabled(account_id: int) -> bool:
    """Return whether this account has exact durable generation evidence."""

    return (
        settings.MARKETPLACE_FEED_RUN_MODE == 'durable'
        and settings.AVITO_STATUS_LIFECYCLE_MODE == 'dual_write'
    ) or private_feed_cutover_enabled(account_id)


def feed_run_may_publish(run: MarketplaceFeedRun | None) -> bool:
    """Return whether provider work may still publish the immutable payload."""

    return run is not None and run.state in MarketplaceFeedRun.OWNERSHIP_STATES


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
        # Legacy delivery has no immutable generation membership.  Be honest
        # about that ambiguity and fail closed for destructive lifecycle edits.
        return ListingDeliveryPresentation(
            stage='legacy_delivery',
            label='Отправляется или обрабатывается Avito',
            provider_submission_started=True,
            lifecycle_actions_blocked=True,
            can_check_avito_status=True,
        )

    state = run.state
    if state == MarketplaceFeedRun.State.PREPARING:
        return ListingDeliveryPresentation(
            stage='feed_preparing',
            label='Фид готовится к отправке',
            provider_submission_started=False,
            lifecycle_actions_blocked=feed_run_may_publish(run),
            can_check_avito_status=False,
        )
    if state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN:
        return ListingDeliveryPresentation(
            stage='submission_unknown',
            label='Проверяем, принял ли Avito фид',
            provider_submission_started=True,
            lifecycle_actions_blocked=feed_run_may_publish(run),
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.POLLING:
        return ListingDeliveryPresentation(
            stage='avito_processing',
            label='Avito обрабатывает фид',
            provider_submission_started=True,
            lifecycle_actions_blocked=feed_run_may_publish(run),
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.REPORTING:
        return ListingDeliveryPresentation(
            stage='avito_reporting',
            label='Получаем результат Avito',
            provider_submission_started=True,
            lifecycle_actions_blocked=feed_run_may_publish(run),
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.RETRY_WAIT:
        return ListingDeliveryPresentation(
            stage='delivery_retry',
            label='Отправка временно задержана, повторяем',
            provider_submission_started=_provider_submission_started(run),
            lifecycle_actions_blocked=feed_run_may_publish(run),
            can_check_avito_status=True,
        )
    if state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN:
        return ListingDeliveryPresentation(
            stage='manual_review',
            label='Результат Avito требует ручной проверки',
            provider_submission_started=True,
            lifecycle_actions_blocked=feed_run_may_publish(run),
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
