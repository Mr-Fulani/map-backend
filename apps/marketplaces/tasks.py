import datetime

from celery import shared_task
from django.core.cache import cache
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


def _queued_publish_listings(account, first_listing: Listing) -> list[Listing]:
    """Возвращает партию queued-листингов аккаунта, начиная с текущего."""
    queued = list(
        Listing.objects.filter(
            account=account,
            status=Listing.STATUS_QUEUED,
            external_id__isnull=True,
        ).select_related('tenant', 'product', 'account').order_by('created_at', 'pk')
    )
    if first_listing.status == Listing.STATUS_QUEUED and first_listing not in queued:
        queued.insert(0, first_listing)
    return queued


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


def _has_active_feed(account) -> bool:
    """У аккаунта уже есть feed, который ждёт ответа Avito."""
    return Listing.objects.filter(
        account=account,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    ).exists()


def _schedule_next_queued_publish(account) -> None:
    """Запускает следующую queued-партию аккаунта, если она есть."""
    next_listing_id = (
        Listing.objects.filter(
            account=account,
            status=Listing.STATUS_QUEUED,
            external_id__isnull=True,
        )
        .order_by('created_at', 'pk')
        .values_list('pk', flat=True)
        .first()
    )
    if next_listing_id:
        publish_listing_task.delay(next_listing_id)


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
        if listing.status not in (Listing.STATUS_QUEUED, Listing.STATUS_DRAFT, Listing.STATUS_REJECTED):
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

        batch = [listing]
        try:
            if _has_active_feed(listing.account):
                _write_log(
                    listing.tenant, 'listing_publish', 'ok',
                    f'«{listing.title or listing.product.name}»'
                    ' поставлен в очередь Avito: ждём обработки предыдущего фида',
                    listing=listing,
                )
                return

            batch = _queued_publish_listings(listing.account, listing)
            # Avito требует контактное лицо и телефон. Не отправляем фид с пустыми
            # контактами — отклоняем такие объявления с понятным пояснением.
            from apps.marketplaces.adapters.avito.feed_builder import (
                get_contact_fields, missing_required_avito_fields, product_has_oem,
            )
            valid_batch = []
            for item in batch:
                manager_name, contact_phone = get_contact_fields(item)
                if not manager_name or not contact_phone:
                    _reject_listing(
                        item,
                        'Не указано контактное лицо и/или телефон. Заполните их в '
                        'профиле аккаунта Avito (Настройки → Маркетплейсы) или в самом '
                        'листинге — Avito не публикует объявления без контактов.',
                    )
                    continue
                # Нет настоящего OEM — публикуем с артикулом, но предупреждаем тенанта.
                if not product_has_oem(item):
                    _write_log(
                        item.tenant, 'listing_publish', 'warn',
                        f'У «{item.title or item.product.name}» нет OEM-номера — '
                        f'в объявление подставлен артикул {item.product.article}. '
                        f'Укажите OEM вручную при необходимости.',
                        listing=item,
                    )
                # Не заполнены обязательные поля категории Avito (напр. размеры шин,
                # вязкость масла) — публикуем, но предупреждаем: Avito может отклонить.
                missing_fields = missing_required_avito_fields(item)
                if missing_fields:
                    _write_log(
                        item.tenant, 'listing_publish', 'warn',
                        f'У «{item.title or item.product.name}» не заполнены поля Avito '
                        f'для его категории: {", ".join(missing_fields)}. '
                        f'Объявление может быть отклонено Avito — заполните их у товара.',
                        listing=item,
                    )
                valid_batch.append(item)
            batch = valid_batch
            if not batch:
                return
            adapter = AvitoAdapter(listing.account)
            if not adapter.is_autoload_active():
                for item in batch:
                    _reject_listing(
                        item,
                        'Автозагрузка Avito не подключена или профиль Autoload недоступен. '
                        'Подключите Автозагрузку в настройках Avito и повторите публикацию.',
                    )
                return
            # Одна автозагрузка = полное состояние аккаунта (активные + Remove),
            # чтобы не затирать другие объявления и не гоняться за лимитом.
            adapter.flush_feed(_account_feed_listings(listing.account))
            Listing.objects.filter(pk__in=[item.pk for item in batch]).update(status=Listing.STATUS_PENDING)
            _write_log(
                listing.tenant, 'listing_publish', 'ok',
                f'Фид загружен для {len(batch)} объявлений {listing.account.name}, ожидаем Avito',
                listing=listing,
            )
            # Запускаем polling результатов
            poll_feed_results_task.apply_async(
                args=[listing.account.pk],
                countdown=300,
            )
        except RateLimitError as exc:
            # Лимит 1/час — повторим после открытия окна, листинги остаются QUEUED.
            raise self.retry(exc=exc, countdown=RATE_LIMIT_RETRY_COUNTDOWN)
        except FeedUploadError as exc:
            listing.retry_count += 1
            if self.request.retries >= self.max_retries:
                for item in batch:
                    item.retry_count += 1
                    item.status = Listing.STATUS_REJECTED
                    item.rejection_reason = str(exc)
                    item.save(update_fields=['status', 'rejection_reason', 'retry_count'])
                    _notify_error(item.tenant, str(exc), listing=item)
                return
            listing.save(update_fields=['retry_count'])
            raise self.retry(exc=exc, countdown=backoff(listing.retry_count))
        except ServerError as exc:
            listing.retry_count += 1
            listing.save(update_fields=['retry_count'])
            raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=6, queue='avito_update')
def update_listing_task(self, listing_id: int):
    """Обновляет содержимое объявления через перезагрузку фида."""
    listing = _get_listing(listing_id)
    if not listing.external_id:
        publish_listing_task.delay(listing_id)
        return
    try:
        _flush_account_or_stop(listing.account)
        listing.last_sync_at = now()
        listing.save(update_fields=['last_sync_at'])
        _write_log(
            listing.tenant, 'listing_update', 'ok',
            f'Фид обновлён для «{listing.title or listing.product.name}»',
            listing=listing,
        )
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=RATE_LIMIT_RETRY_COUNTDOWN)
    except (FeedUploadError, ServerError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


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
    listing.status = Listing.STATUS_ARCHIVING
    listing.save(update_fields=['status'])
    try:
        # Полный фид аккаунта без этого объявления → Avito его архивирует.
        _flush_account_or_stop(listing.account)
        _write_log(
            listing.tenant, 'listing_unpublish', 'ok',
            f'Запрос на снятие «{listing.title or listing.product.name}» отправлен в Avito',
            listing=listing,
        )
        # Подтверждение снятия опросом статуса (Avito обрабатывает пакетно).
        confirm_removal_task.apply_async(args=[listing.pk], countdown=300)
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=RATE_LIMIT_RETRY_COUNTDOWN)
    except (FeedUploadError, ServerError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


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
    try:
        # Полный фид аккаунта: Remove этого объявления + остальные активные.
        _flush_account_or_stop(listing.account)
        _write_log(
            listing.tenant, 'listing_delete', 'ok',
            f'Запрос на удаление «{listing.title or listing.product.name}» отправлен в Avito',
            listing=listing,
        )
        poll_feed_results_task.apply_async(args=[listing.account.pk], countdown=300)
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=RATE_LIMIT_RETRY_COUNTDOWN)
    except (FeedUploadError, ServerError) as exc:
        raise self.retry(exc=exc, countdown=backoff(listing.retry_count))


@shared_task(bind=True, max_retries=5, queue='avito_publish')
def flush_feed_task(self, account_id: int):
    """
    Собирает все PENDING листинги аккаунта и загружает один батч-фид.

    Запускается по расписанию (каждые 10 мин) или вручную после накопления изменений.
    """
    from apps.marketplaces.models import MarketplaceAccount
    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(pk=account_id)
    except MarketplaceAccount.DoesNotExist:
        return

    listings = list(
        Listing.objects.filter(
            account=account,
            status=Listing.STATUS_PENDING,
            external_id__isnull=True,
        ).select_related('tenant', 'product')
    )
    if not listings:
        return

    try:
        _flush_account_or_stop(account)
        _write_log(
            account.tenant, 'feed_flush', 'ok',
            f'Фид загружен: {len(listings)} объявлений для {account.name}',
        )
        poll_feed_results_task.apply_async(args=[account_id], countdown=300)
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=RATE_LIMIT_RETRY_COUNTDOWN)
    except (FeedUploadError, ServerError) as exc:
        if self.request.retries >= self.max_retries:
            reason = str(exc)
            for listing in listings:
                _reject_listing(listing, reason)
            return
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))


@shared_task(bind=True, max_retries=10, queue='avito_publish')
def poll_feed_results_task(self, account_id: int):
    """
    Опрашивает Avito Autoload о результатах обработки фида.

    Сопоставляет ad_id (publish_idempotency_key) с avito_id и обновляет
    Listing.external_id + status='active' для опубликованных объявлений.

    Запускается через 10 мин после flush_feed_task; при необходимости повторяет.
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
        _schedule_next_queued_publish(account)
        return

    ad_ids = [get_ad_id(lst) for lst in pending]
    try:
        results = AvitoAdapter(account).get_feed_results(ad_ids)
    except FeedUploadError as exc:
        reason = str(exc)
        for listing in pending:
            _reject_listing(listing, reason)
        _schedule_next_queued_publish(account)
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
            _schedule_next_queued_publish(account)
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
    _schedule_next_queued_publish(account)


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
def setup_autoload_profile_task(self, account_id: int):
    """
    Регистрирует feed URL тенанта в профиле Avito Autoload.

    Вызывается автоматически после создания MarketplaceAccount.
    Использует email владельца тенанта как report_email.
    При ошибке делает retry — не блокирует создание аккаунта.
    """
    from apps.marketplaces.models import MarketplaceAccount
    from apps.tenants.models import TenantUser

    try:
        account = MarketplaceAccount.objects.select_related('tenant').get(pk=account_id)
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
        _write_log(
            account.tenant, 'autoload_profile_setup', 'ok',
            f'Autoload профиль Avito настроен для {account.name}',
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=backoff(self.request.retries))
