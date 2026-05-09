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

    try:
        result = DescriptionAgent().generate(product, tenant)
        return {
            'product_id': product_id,
            'title': result['title'],
            'confidence': result['confidence'],
        }
    except AICreditsExhausted as exc:
        return {'skipped': True, 'reason': str(exc)}
    except Exception as exc:
        raise self.retry(exc=exc)
