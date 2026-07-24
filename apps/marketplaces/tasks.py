import datetime

from celery import shared_task
from django.core.cache import cache
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now

from apps.anti_ban.ramp_up import GradualRampUp
from apps.anti_ban.velocity import VelocityController
from apps.billing.services import LimitChecker
from apps.marketplaces.adapters.avito.adapter import AvitoAdapter, FeedUploadError
from apps.marketplaces.adapters.avito.error_handler import (
    ServerError,
    backoff,
)
from apps.marketplaces.adapters.avito.feed_builder import get_ad_id
from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError
from apps.marketplaces.models import Listing
from apps.notifications.services import LEVEL_CRITICAL, LEVEL_ERROR, LEVEL_SUCCESS


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


def _reject_listing(listing: Listing, reason: str) -> None:
    listing.status = Listing.STATUS_REJECTED
    listing.rejection_reason = reason
    listing.last_sync_at = now()
    listing.save(update_fields=['status', 'rejection_reason', 'last_sync_at'])
    _notify_error(listing.tenant, reason, listing=listing)


def _send_listing_to_review(listing: Listing, reason: str) -> None:
    """Отправляет листинг на проверку (вкладка «Требуют проверки») с причиной.

    В отличие от _reject_listing статус — requires_review: тенант исправляет
    данные и жмёт «Одобрить и опубликовать» (ListingService.approve).
    """
    listing.status = Listing.STATUS_REQUIRES_REVIEW
    listing.rejection_reason = reason
    listing.last_sync_at = now()
    listing.save(update_fields=['status', 'rejection_reason', 'last_sync_at'])
    _notify_error(listing.tenant, reason, listing=listing)


# Пауза перед повтором при лимите Avito «1 автозагрузка/час» (~11 минут).
RATE_LIMIT_RETRY_COUNTDOWN = 660


def _account_feed_listings(account) -> list:
    """
    Полное состояние фида аккаунта в одной автозагрузке (фид-координатор):
    ВСЕ объявления, которые должны быть активны (active/pending/queued).

    Avito тянет один URL фида на аккаунт. Снятие делается ОТСУТСТВИЕМ объявления
    в файле (Avito архивирует то, чего нет), поэтому archived/deleted сюда НЕ
    включаем — они уйдут в архив на стороне Avito.
    """
    return list(
        Listing.objects.filter(
            account=account,
            status__in=[Listing.STATUS_ACTIVE, Listing.STATUS_PENDING, Listing.STATUS_QUEUED],
        ).select_related('tenant', 'product', 'account').order_by('created_at', 'pk')
    )


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
    with cache.lock(lock_key, timeout=60):
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
            listing.external_id = None
            listing.save(update_fields=['external_id'])
        if listing.status != Listing.STATUS_QUEUED:
            listing.status = Listing.STATUS_QUEUED
            listing.save(update_fields=['status'])

        can, reason = LimitChecker().can_publish(listing.tenant)
        if not can:
            listing.status = Listing.STATUS_LIMIT_REACHED
            listing.save(update_fields=['status'])
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
        listing.save(update_fields=['status'])
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
        listing.status = Listing.STATUS_ARCHIVED
        listing.save(update_fields=['status'])
        return
    # Промежуточный статус «Снимается» — в «В архиве» переведём после подтверждения.
    # Снятие = отсутствие объявления в фиде; фид уйдёт ближайшим часовым окном,
    # а check_moderation_status дожмёт подтверждение снятия (confirm_removal_task).
    listing.status = Listing.STATUS_ARCHIVING
    listing.save(update_fields=['status'])
    _write_log(
        listing.tenant, 'listing_unpublish', 'ok',
        f'«{listing.title or listing.product.name}» будет снято с публикации ближайшим фидом Avito',
        listing=listing,
    )
    request_feed_flush(listing.account)


@shared_task(bind=True, max_retries=3, queue='avito_update')
def confirm_removal_task(self, listing_id: int):
    """
    Подтверждает снятие: переводит «Снимается» → «В архиве», когда Avito
    перестал показывать объявление активным (autoload обрабатывает пакетно).
    """
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
    Listing.objects.filter(
        account=account, status=Listing.STATUS_QUEUED, external_id__isnull=True,
    ).update(status=Listing.STATUS_PENDING)

    pending = list(
        Listing.objects.filter(
            account=account,
            status=Listing.STATUS_PENDING,
            external_id__isnull=True,
        ).select_related('tenant', 'product')
    )
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


@shared_task(bind=True, max_retries=10, queue='avito_publish')
def poll_feed_results_task(self, account_id: int):
    """
    Опрашивает Avito Autoload о результатах обработки фида.

    Сопоставляет ad_id (publish_idempotency_key) с avito_id и обновляет
    Listing.external_id + status='active' для опубликованных объявлений.

    Запускается через 5 мин после coalesced_flush_task; при необходимости повторяет.
    """
    from apps.marketplaces.models import MarketplaceAccount
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
        reason = item_errors.get(get_ad_id(listing))
        if reason:
            _reject_listing(listing, reason)
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
            _write_log(
                listing.tenant, 'moderation', 'ok',
                f'Объявление «{listing.title or listing.product.name}» прошло модерацию Avito',
                listing=listing,
            )
        elif avito_status in ('rejected', 'blocked'):
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = data.get('rejection_reason', '')
            reason_txt = f': {listing.rejection_reason}' if listing.rejection_reason else ''
            _notify_error(
                listing.tenant,
                f'Объявление «{listing.title or listing.product.name}» отклонено при модерации Avito'
                + reason_txt,
                listing=listing,
            )
            _write_log(
                listing.tenant, 'moderation', 'warn',
                f'Отклонено модерацией{reason_txt}',
                listing=listing,
            )
        listing.last_sync_at = now()
        listing.save(update_fields=['status', 'rejection_reason', 'last_sync_at'])
    except (ServerError, RateLimitError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


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
    lock = cache.lock(lock_key, timeout=120)
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
