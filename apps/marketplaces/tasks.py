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
            listing.external_id = None
            listing.status = Listing.STATUS_DRAFT
        except RejectedError as exc:
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = exc.reason
        except RateLimitError as exc:
            listing.retry_count += 1
            listing.next_retry_at = now()
            listing.save(update_fields=['status', 'retry_count', 'next_retry_at'])
            raise self.retry(exc=exc, countdown=exc.retry_after)
        except (ServerError, Exception) as exc:
            listing.retry_count += 1
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
        listing.last_sync_at = now()
        listing.save(update_fields=['status', 'rejection_reason', 'last_sync_at'])
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))
