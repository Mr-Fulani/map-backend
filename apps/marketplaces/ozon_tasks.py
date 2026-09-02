"""Bounded Ozon automation, isolated from the protected Avito task module."""

import uuid

from celery import shared_task
from django.utils import timezone
from django.db.models import Q

from apps.marketplaces.models import OzonAccountProfile, OzonOfferDraft, OzonOperation
from apps.marketplaces.ozon_commerce import OzonCommerceError, sync_offer_commerce
from apps.marketplaces.ozon_orders import OzonOrderSyncError, sync_fbs_orders
from apps.marketplaces.ozon_reconciliation import OzonReconciliationError, reconcile_product_import


@shared_task(queue='sync_import')
def reconcile_due_ozon_imports():
    operations = OzonOperation.objects.filter(
        kind=OzonOperation.Kind.PRODUCT_IMPORT,
        state__in=OzonOperation.ACTIVE_STATES,
        account__is_active=True,
    ).filter(
        Q(next_reconcile_at__isnull=True) | Q(next_reconcile_at__lte=timezone.now()),
    ).select_related('offer__product', 'account')[:100]
    completed = 0
    for operation in operations:
        try:
            reconcile_product_import(operation.offer.product, operation.account)
            completed += 1
        except OzonReconciliationError:
            continue
    return {'checked': completed}


@shared_task(queue='sync_import')
def sync_enabled_ozon_commerce():
    drafts = OzonOfferDraft.objects.filter(
        account__is_active=True,
        account__ozon_profile__commerce_auto_sync_enabled=True,
        account__ozon_profile__product_write_enabled=True,
        publication_status='published',
        provider_product_id__isnull=False,
    ).select_related('product', 'account').order_by('pk')[:100]
    checked = 0
    for draft in drafts:
        try:
            operation_key = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f'ozon-commerce:{draft.pk}:{timezone.now():%Y%m%d%H%M}',
            )
            sync_offer_commerce(
                draft.product, draft.account,
                idempotency_key=str(operation_key),
            )
            checked += 1
        except OzonCommerceError:
            continue
    return {'checked': checked}


@shared_task(queue='sync_import')
def sync_enabled_ozon_orders():
    profiles = OzonAccountProfile.objects.filter(
        account__is_active=True, orders_auto_sync_enabled=True,
    ).select_related('account').order_by('pk')[:20]
    checked = 0
    for profile in profiles:
        try:
            sync_fbs_orders(profile.account)
            checked += 1
        except OzonOrderSyncError:
            continue
    return {'checked': checked}
