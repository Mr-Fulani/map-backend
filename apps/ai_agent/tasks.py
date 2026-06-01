from celery import shared_task

from apps.ai_agent.services import AICreditsExhausted, DescriptionAgent
from apps.billing.services import LimitChecker
from apps.products.models import Product


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
        return {'skipped': True, 'reason': reason}

    from apps.products.services import ProductKnowledgeGraphService
    applied_knowledge = ProductKnowledgeGraphService.apply_known_knowledge_to_product(product)
    if applied_knowledge['relations_count'] or applied_knowledge['fitments_count']:
        product.refresh_from_db()

    try:
        result = DescriptionAgent().generate(product, tenant)
    except AICreditsExhausted as exc:
        return {'skipped': True, 'reason': str(exc)}
    except Exception as exc:
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

    return {
        'product_id': product_id,
        'title': result['title'],
        'confidence': result['confidence'],
        **applied_knowledge,
    }
