from celery import shared_task

from apps.ai_agent.services import AICreditsExhausted, DescriptionAgent
from apps.billing.services import LimitChecker
from apps.products.models import Product


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


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             retry_backoff=True, queue='ai_generate')
def generate_description_task(self, product_id: int):
    try:
        product = Product.objects.select_related('tenant').get(pk=product_id)
    except Product.DoesNotExist:
        return {'error': f'Product {product_id} not found'}

    tenant = product.tenant
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
    except Exception as exc:
        _write_sync_log(tenant, 'error', f'Ошибка генерации AI для товара #{product_id}: {exc}')
        raise self.retry(exc=exc)

    # Сохраняем на уровень Product (всегда видно на странице товара)
    Product.objects.filter(pk=product_id).update(
        title_ai=result['title'],
        description_ai=result['description'],
    )

    # Синхронизируем листинги товара если они есть
    from apps.marketplaces.models import Listing
    _regenerable = (
        Listing.STATUS_DRAFT,
        Listing.STATUS_REQUIRES_REVIEW,
        Listing.STATUS_REJECTED,
    )
    Listing.objects.filter(product_id=product_id, status__in=_regenerable).update(
        title=result['title'],
        description_ai=result['description'],
        ai_confidence=result['confidence'],
    )

    _write_sync_log(
        tenant, 'ok',
        f'Описание сгенерировано для «{result["title"]}», confidence={result["confidence"]:.2f}',
    )
    return {
        'product_id': product_id,
        'title': result['title'],
        'confidence': result['confidence'],
        **applied_knowledge,
    }
