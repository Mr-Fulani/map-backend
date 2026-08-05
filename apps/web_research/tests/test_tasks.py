from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.products.models import Product
from apps.tenants.services import TenantService
from apps.web_research.models import WebResearchRun
from apps.web_research.tasks import schedule_web_research_fallback


@pytest.mark.django_db
def test_sparse_product_schedules_web_research_fallback():
    tenant, _ = TenantService.create_tenant(
        'web-fallback', 'web-fallback', 'web-fallback@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='OEM0099FONR',
        name='Фонарь правый внешний Kia Optima JF',
        category_1c='Автосвет',
        price=Decimal('0'),
    )

    with patch('apps.web_research.tasks.run_web_research.delay') as delay:
        result = schedule_web_research_fallback.run(product.pk)

    run = WebResearchRun.objects.get(product=product)
    assert run.trigger == WebResearchRun.Trigger.PARSER_FALLBACK
    assert result['run_id'] == run.pk
    delay.assert_called_once_with(run.pk)
