from celery import shared_task

from apps.ai_agent.services import AICreditsExhausted, DescriptionAgent
from apps.billing.services import LimitChecker
from apps.products.models import Product


@shared_task(queue='notifications', ignore_result=True)
def reconcile_stale_ai_provider_operations_task(limit: int = 200):
    """Recover AI wallet reservations orphaned by a killed worker."""
    from apps.ai_agent.reconciliation import (
        apply_pending_ai_provider_results,
        reconcile_stale_ai_provider_operations,
    )
    return {
        'reservations': reconcile_stale_ai_provider_operations(limit=limit),
        'results': apply_pending_ai_provider_results(limit=limit),
    }


def _write_sync_log(tenant, status: str, message: str) -> None:
    """Записывает событие description_gen в SyncLog — не падает при ошибках."""
    try:
        from apps.sync.models import SyncLog
        SyncLog.objects.create(
            tenant=tenant,
            event_type=SyncLog.EVENT_DESCRIPTION_GEN,
            status=status,
            message=message,
        )
    except Exception:
        pass


def _schedule_ozon_autofill(product_id: int, trigger_key: str) -> None:
    try:
        from apps.marketplaces.ozon_autofill import schedule_ozon_autofill

        schedule_ozon_autofill(product_id, trigger_key=trigger_key)
    except Exception:
        # The AI result is already paid and applied. Ozon preparation is a
        # follow-up and must not roll it back or mark generation as failed.
        pass


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='ai_generate')
def generate_description_task(self, product_id: int):
    try:
        product = Product.objects.select_related('tenant').get(pk=product_id)
    except Product.DoesNotExist:
        return {'error': f'Product {product_id} not found'}

    tenant = product.tenant
    from apps.ai_agent.reconciliation import (
        AIProviderReconciliationRequired,
        apply_description_provider_operation,
        pending_description_operation_id,
    )
    pending_operation_id = pending_description_operation_id(
        tenant_id=tenant.pk,
        product_id=product.pk,
    )
    if pending_operation_id is not None:
        result = apply_description_provider_operation(pending_operation_id)
        _schedule_ozon_autofill(
            product_id,
            f'ai-operation:{pending_operation_id}',
        )
        _write_sync_log(
            tenant,
            'ok',
            f'Восстановлен оплаченный AI-результат для товара #{product_id}.',
        )
        return {
            'product_id': product_id,
            'title': result['title'],
            'confidence': result['confidence'],
            'provider_operation_id': str(pending_operation_id),
            'resumed': True,
        }

    can, reason = LimitChecker().can_generate_ai(tenant)
    if not can:
        _write_sync_log(tenant, 'warn', f'Лимит AI-кредитов исчерпан: {reason}')
        return {'skipped': True, 'reason': reason}

    from apps.products.services import ProductKnowledgeGraphService
    applied_knowledge = ProductKnowledgeGraphService.apply_known_knowledge_to_product(product)
    if applied_knowledge['relations_count'] or applied_knowledge['fitments_count']:
        product.refresh_from_db()

    try:
        result = DescriptionAgent().generate(product, tenant)
    except AICreditsExhausted as exc:
        _write_sync_log(tenant, 'warn', str(exc))
        return {'skipped': True, 'reason': str(exc)}
    except AIProviderReconciliationRequired:
        reason = 'provider_reconciliation_required'
        _write_sync_log(
            tenant,
            'warn',
            f'Генерация товара #{product_id} ожидает сверки '
            'прошлого AI-запроса.',
        )
        return {'skipped': True, 'reason': reason}
    except Exception as exc:
        _write_sync_log(tenant, 'error', f'Ошибка генерации AI для товара #{product_id}: {exc}')
        # This task is delivered exclusively through BackgroundJobDispatch.
        # A Celery-native retry here would create a second, untracked delivery
        # outside the durable lease/accounting state machine.  Propagate the
        # original exception so the single durable executor can classify it.
        raise

    operation_id = result.pop('_provider_operation_id')
    result = apply_description_provider_operation(operation_id)
    _schedule_ozon_autofill(product_id, f'ai-operation:{operation_id}')

    # Product/Listing writes and the exact operation's applied marker commit in
    # one transaction inside apply_description_provider_operation().
    product.refresh_from_db(fields=['title_ai', 'description_ai'])
    result['title'] = product.title_ai
    result['description'] = product.description_ai
    result['provider_operation_id'] = str(operation_id)

    _write_sync_log(
        tenant, 'ok',
        f'Описание сгенерировано для «{result["title"]}», confidence={result["confidence"]:.2f}',
    )
    return {
        'product_id': product_id,
        'title': result['title'],
        'confidence': result['confidence'],
        'provider_operation_id': str(operation_id),
        **applied_knowledge,
    }
