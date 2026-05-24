from celery import shared_task
from django.core.cache import cache
from django.utils.timezone import now

from apps.anti_ban.ramp_up import GradualRampUp
from apps.anti_ban.velocity import VelocityController
from apps.billing.services import LimitChecker
from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.adapters.avito.error_handler import (
    NotFoundError,
    RejectedError,
    ServerError,
    TokenExpiredError,
    backoff,
)
from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError
from apps.marketplaces.models import Listing
from apps.notifications.services import LEVEL_CRITICAL, LEVEL_ERROR


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


def _notify_critical(tenant, message: str) -> None:
    """Асинхронно отправляет critical-уведомление тенанту."""
    from apps.notifications.tasks import send_notification_task
    send_notification_task.delay(tenant.pk, LEVEL_CRITICAL, message)


def _get_listing(listing_id: int) -> Listing:
    return Listing.objects.select_related('tenant', 'product', 'account').get(pk=listing_id)


@shared_task(bind=True, max_retries=3, queue='avito_publish')
def publish_listing_task(self, listing_id: int):
    listing = _get_listing(listing_id)

    if listing.external_id:
        return

    lock_key = f'avito:publish_lock:{listing.publish_idempotency_key}'
    with cache.lock(lock_key, timeout=60):
        listing.refresh_from_db()
        if listing.external_id:
            return

        can, reason = LimitChecker().can_publish(listing.tenant)
        if not can:
            listing.status = Listing.STATUS_LIMIT_REACHED
            listing.save(update_fields=['status'])
            _notify_critical(
                listing.tenant,
                f'Достигнут лимит публикаций: {reason}. Новые объявления заблокированы.',
            )
            return

        # Проверка gradual ramp-up — лимит публикаций в первые дни работы тенанта
        ramp = GradualRampUp()
        published_today = ramp.get_published_today(listing.tenant)
        if not ramp.is_allowed(listing.tenant, published_today):
            # Откладываем задачу на следующий день, не теряем
            raise self.retry(exc=RuntimeError('Ramp-up limit reached'), countdown=3600)

        # Проверка velocity — защита от слишком быстрых публикаций
        if not VelocityController().is_allowed(listing.account, 'publish'):
            raise self.retry(exc=RuntimeError('Velocity limit exceeded'), countdown=300)

        try:
            external_id = AvitoAdapter(listing.account).publish(listing)
            listing.external_id = external_id
            listing.status = Listing.STATUS_ACTIVE
            listing.published_at = now()
        except TokenExpiredError as exc:
            raise self.retry(exc=exc, countdown=5)
        except NotFoundError:
            # При первичной публикации 404 означает ошибку аккаунта (неверный user_id или нет доступа)
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = 'Аккаунт не найден в Avito API. Проверьте User ID аккаунта.'
            _notify_error(
                listing.tenant,
                f'Не удалось опубликовать «{listing.title or listing.product.name}»: '
                f'аккаунт {listing.account.name!r} не найден в Avito API (ошибка 404). '
                f'Проверьте User ID в настройках аккаунта.',
                listing=listing,
            )
        except RejectedError as exc:
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = exc.reason
            _notify_error(
                listing.tenant,
                f'Объявление «{listing.title or listing.product.name}» отклонено Avito: {exc.reason}',
                listing=listing,
            )
        except RateLimitError as exc:
            listing.retry_count += 1
            listing.next_retry_at = now()
            listing.save(update_fields=['status', 'retry_count', 'next_retry_at'])
            raise self.retry(exc=exc, countdown=exc.retry_after)
        except (ServerError, Exception) as exc:
            listing.retry_count += 1
            if self.request.retries >= self.max_retries:
                listing.status = Listing.STATUS_REJECTED
                listing.rejection_reason = f'Ошибка сервера после {self.max_retries} попыток: {exc}'
                _notify_error(
                    listing.tenant,
                    f'Не удалось опубликовать «{listing.title or listing.product.name}» '
                    f'после {self.max_retries} попыток: {exc}',
                    listing=listing,
                )
            raise self.retry(exc=exc, countdown=backoff(listing.retry_count))
        finally:
            listing.save()


@shared_task(bind=True, max_retries=3, queue='avito_update')
def update_listing_task(self, listing_id: int):
    listing = _get_listing(listing_id)
    if not listing.external_id:
        publish_listing_task.delay(listing_id)
        return
    try:
        AvitoAdapter(listing.account).update(listing)
        listing.last_sync_at = now()
        listing.save(update_fields=['last_sync_at'])
    except NotFoundError:
        listing.external_id = None
        listing.status = Listing.STATUS_DRAFT
        listing.save(update_fields=['external_id', 'status'])
        publish_listing_task.delay(listing_id)
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=5, queue='avito_price')
def update_price_task(self, listing_id: int):
    listing = _get_listing(listing_id)
    if not listing.external_id:
        return
    try:
        AvitoAdapter(listing.account).update_price(listing)
        listing.last_sync_at = now()
        listing.save(update_fields=['last_sync_at'])
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=3, queue='avito_delete')
def unpublish_listing_task(self, listing_id: int):
    listing = _get_listing(listing_id)
    if not listing.external_id:
        return
    try:
        AvitoAdapter(listing.account).unpublish(listing)
        listing.status = Listing.STATUS_ARCHIVED
        listing.save(update_fields=['status'])
    except NotFoundError:
        listing.status = Listing.STATUS_ARCHIVED
        listing.save(update_fields=['status'])
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=3, queue='avito_delete')
def delete_listing_task(self, listing_id: int):
    listing = _get_listing(listing_id)
    if not listing.external_id:
        listing.status = Listing.STATUS_DELETED
        listing.save(update_fields=['status'])
        return
    try:
        AvitoAdapter(listing.account).delete(listing)
        listing.status = Listing.STATUS_DELETED
        listing.save(update_fields=['status'])
    except NotFoundError:
        listing.status = Listing.STATUS_DELETED
        listing.save(update_fields=['status'])
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=3, queue='avito_update')
def check_moderation_task(self, listing_id: int):
    listing = _get_listing(listing_id)
    if not listing.external_id:
        return
    try:
        data = AvitoAdapter(listing.account).get_status(listing)
        avito_status = data.get('status', '')
        if avito_status == 'active':
            listing.status = Listing.STATUS_ACTIVE
        elif avito_status in ('rejected', 'blocked'):
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = data.get('rejection_reason', '')
            _notify_error(
                listing.tenant,
                f'Объявление «{listing.title or listing.product.name}» отклонено при модерации Avito'
                + (f': {listing.rejection_reason}' if listing.rejection_reason else '.'),
                listing=listing,
            )
        listing.last_sync_at = now()
        listing.save(update_fields=['status', 'rejection_reason', 'last_sync_at'])
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(queue='avito_update')
def check_moderation_status():
    """
    Запускает проверку статуса модерации для всех активных листингов.

    Запускается каждые 30 минут через Celery Beat.
    """
    listing_ids = list(Listing.objects.filter(
        status=Listing.STATUS_ACTIVE,
    ).values_list('pk', flat=True))

    for listing_id in listing_ids:
        check_moderation_task.delay(listing_id)

    return {'listings_queued': len(listing_ids)}


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
    Запускает проверку теневого бана для всех активных аккаунтов.

    Запускается ежечасно через Celery Beat.
    """
    from apps.anti_ban.tasks import check_shadow_ban_task
    from apps.marketplaces.models import MarketplaceAccount

    account_ids = list(MarketplaceAccount.objects.filter(
        is_active=True,
    ).values_list('pk', flat=True))

    for account_id in account_ids:
        check_shadow_ban_task.delay(account_id)

    return {'accounts_checked': len(account_ids)}
