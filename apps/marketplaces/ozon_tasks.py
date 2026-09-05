"""Bounded, fair Ozon automation with durable per-account cursors."""

from datetime import timedelta
import logging
import time
import uuid
from typing import Any

from celery import shared_task
from billiard.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from apps.core.advisory_lock import try_session_advisory_lock
from apps.marketplaces.models import OzonAccountProfile, OzonOfferDraft, OzonOperation
from apps.marketplaces.ozon_commerce import OzonCommerceError, sync_offer_commerce
from apps.marketplaces.ozon_events import record_automation_result
from apps.marketplaces.ozon_lifecycle import OzonLifecycleError, reconcile_product_archive
from apps.marketplaces.ozon_orders import OzonOrderSyncError, sync_fbs_orders
from apps.marketplaces.ozon_reconciliation import OzonReconciliationError, reconcile_product_import


logger = logging.getLogger(__name__)
ACCOUNT_BATCH_SIZE = 20
ITEM_BATCH_SIZE = 100
RUN_BUDGET_SECONDS = 45
EXPECTED_ERRORS = (OzonCommerceError, OzonLifecycleError, OzonOrderSyncError, OzonReconciliationError)


def _result_error(value):
    operations = value if isinstance(value, tuple) else (value,)
    for operation in operations:
        if not isinstance(operation, OzonOperation):
            continue
        if operation.state in {'failed', 'partial', 'manual_review', 'outcome_unknown'}:
            return 'sync_failed'
        if operation.state == 'reconciling' and operation.response_summary.get('last_status_error'):
            return 'sync_failed'
    return ''


def _items(job, account_id, tenant_id):
    if job == 'commerce':
        return OzonOfferDraft.objects.filter(
            account_id=account_id, tenant_id=tenant_id, product__tenant_id=tenant_id,
            publication_status='published', provider_product_id__isnull=False,
        )
    return OzonOperation.objects.filter(
        account_id=account_id, tenant_id=tenant_id,
        offer__account_id=account_id, offer__tenant_id=tenant_id,
        offer__product__tenant_id=tenant_id,
        kind__in=(OzonOperation.Kind.PRODUCT_IMPORT, OzonOperation.Kind.ARCHIVE),
        state__in=OzonOperation.ACTIVE_STATES,
    ).filter(Q(next_reconcile_at__isnull=True) | Q(next_reconcile_at__lte=timezone.now()))


def _eligible_profiles(job, before):
    field = f'{job}_checked_at'
    profiles = OzonAccountProfile.objects.filter(
        account__is_active=True, account__marketplace='ozon',
    ).filter(Q(**{f'{field}__isnull': True}) | Q(**{f'{field}__lte': before}))
    if job == 'orders':
        return profiles.filter(orders_auto_sync_enabled=True)
    if job == 'commerce':
        profiles = profiles.filter(commerce_auto_sync_enabled=True, product_write_enabled=True)
    return profiles.filter(Exists(_items(job, OuterRef('account_id'), OuterRef('account__tenant_id'))))


def _round_robin_rows(queryset, cursor, limit):
    """Two indexed keyset reads; wrap once without revisiting a row in a batch."""
    rows = list(queryset.filter(pk__gt=cursor).order_by('pk')[:limit]) if cursor else []
    if not cursor:
        return list(queryset.order_by('pk')[:limit])
    if len(rows) < limit:
        rows.extend(queryset.filter(pk__lte=cursor).order_by('pk')[:limit - len(rows)])
    return rows


def _visit(profile, job, limit, deadline):
    checked = failed = attempted = 0
    code = ''
    rows: list[Any]
    outcome: Any
    if job == 'orders':
        rows = [None]
    else:
        queryset = _items(job, profile.account_id, profile.account.tenant_id)
        queryset = queryset.select_related('product' if job == 'commerce' else 'offer__product')
        rows = _round_robin_rows(queryset, getattr(profile, f'{job}_cursor'), limit)
    for row in rows:
        if time.monotonic() >= deadline:
            break
        # Recheck switches between requests too; the user can turn automation
        # off while a long account batch is running.
        if not _eligible_profiles(job, timezone.now()).filter(pk=profile.pk).exists():
            break
        if row is not None:
            # Advance even on errors or a worker crash, so a broken product
            # cannot permanently starve the remaining cards in this account.
            OzonAccountProfile.objects.filter(pk=profile.pk).update(**{f'{job}_cursor': row.pk})
        attempted += 1
        try:
            if job == 'orders':
                outcome = sync_fbs_orders(profile.account)
            elif job == 'commerce':
                outcome = sync_offer_commerce(
                    row.product, profile.account,
                    idempotency_key=str(uuid.uuid4()),
                )
            elif row.kind == OzonOperation.Kind.ARCHIVE:
                outcome = reconcile_product_archive(row.offer.product, profile.account)
            else:
                outcome = reconcile_product_import(row.offer.product, profile.account)
            checked += 1
            result_error = _result_error(outcome)
            if result_error:
                failed += 1
                code = result_error
        except EXPECTED_ERRORS as exc:
            failed += 1
            code = exc.code
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            # Preserve fairness if one account has bad data or an unexpected
            # failure. Never log exception text (it can include credentials).
            failed += 1
            code = 'unexpected_error'
            logger.error('Ozon automation unexpected failure job=%s account_id=%s', job, profile.account_id)
    if attempted:
        with transaction.atomic():
            record_automation_result(profile, job, checked=checked, failed=failed, code=code)
    return checked, failed, attempted


def _run(job):
    started = time.monotonic()
    interval = timedelta(seconds=50 if job == 'reconciliation' else 290)
    before = timezone.now() - interval
    field = f'{job}_checked_at'
    profile_ids = list(_eligible_profiles(job, before).order_by(
        F(field).asc(nulls_first=True), 'pk',
    ).values_list('pk', flat=True)[:ACCOUNT_BATCH_SIZE])
    result = {'checked': 0, 'failed': 0, 'attempted': 0, 'accounts': 0, 'busy': 0}
    # One small account still gets the full item budget; many accounts share it.
    quota = max(1, ITEM_BATCH_SIZE // max(1, len(profile_ids)))
    for profile_id in profile_ids:
        if time.monotonic() - started >= RUN_BUDGET_SECONDS:
            break
        with try_session_advisory_lock(f'ozon:automation:account:{profile_id}') as acquired:
            if not acquired:
                result['busy'] += 1
                continue
            # A competing dispatcher or a changed switch may invalidate the
            # snapshot. The session lock spans the committed cursor and I/O.
            profile = _eligible_profiles(job, before).select_related('account').filter(pk=profile_id).first()
            if profile is None:
                continue
            OzonAccountProfile.objects.filter(pk=profile.pk).update(**{field: timezone.now()})
            result['accounts'] += 1
            checked, failed, attempted = _visit(profile, job, quota, started + RUN_BUDGET_SECONDS)
            result['checked'] += checked
            result['failed'] += failed
            result['attempted'] += attempted
    logger.info('Ozon automation job=%s result=%s', job, result)
    return result


@shared_task(queue='sync_import')
def reconcile_due_ozon_imports():
    return _run('reconciliation')


@shared_task(queue='sync_import')
def sync_enabled_ozon_commerce():
    return _run('commerce')


@shared_task(queue='sync_import')
def sync_enabled_ozon_orders():
    return _run('orders')
