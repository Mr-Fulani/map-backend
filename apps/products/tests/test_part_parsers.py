from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.products.models import Product, ProductCrossCode
from apps.products.part_parsers import ParsedPart, TachkaPartParser, parse_fitment_line
from apps.products.services import ProductEnrichmentService
from apps.tenants.services import TenantService


SAMPLE_HTML = """
<html>
  <body>
    <h1>Brembo P50136 Колодки тормозные задние MERCEDES W213</h1>
    <table>
      <tr><th>Ширина</th><td>114 мм</td></tr>
      <tr><th>Толщина</th><td>16 мм</td></tr>
      <tr><th>OEM MERCEDES-BENZ</th><td>A 000 420 60 00</td></tr>
      <tr><th>Аналог</th><td>FDB5032</td></tr>
    </table>
    <div class="description">Колодки для задней оси с тормозной системой TRW.</div>
    <p>E-CLASS (W213) 01.2016-2023 E 220 d 4-matic (213.005) 194 л.с</p>
    <img src="/images/p50136.jpg" />
  </body>
</html>
"""


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_product(tenant):
    return Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price=Decimal('1234.00'),
        stock_qty=7,
        warehouse='Основной',
    )


def test_tachka_parser_extracts_enrichment_data_from_html():
    parsed = TachkaPartParser().parse_html(
        SAMPLE_HTML,
        brand='BREMBO',
        article='P 50 136',
        source_url='https://tachka.ru/example',
    )

    assert parsed.normalized_article == 'P50136'
    assert parsed.title.startswith('Brembo P50136')
    assert parsed.attributes['Ширина'] == '114 мм'
    assert parsed.cross_codes[0].code == 'A 000 420 60 00'
    assert parsed.cross_codes[0].code_type == ProductCrossCode.CodeType.OEM
    assert parsed.fitments[0].model == 'E-CLASS'
    assert parsed.fitments[0].generation == 'W213'
    assert parsed.fitments[0].engine_code == '213.005'
    assert parsed.fitments[0].power_hp == 194
    assert parsed.image_urls == ['https://tachka.ru/images/p50136.jpg']
    assert 'description' in parsed.description_facts


def test_parse_fitment_line_keeps_uncertain_data_reviewable():
    fitment = parse_fitment_line(
        'E-CLASS (W213) 01.2016-2023 E 220 d 4-matic (213.005) 194 л.с'
    )

    assert fitment.model == 'E-CLASS'
    assert fitment.date_from == '01.2016'
    assert fitment.date_to == '2023'
    assert fitment.needs_review is False


@pytest.mark.django_db
def test_save_parsed_part_does_not_touch_commercial_product_fields():
    tenant = make_tenant('parsed-save')
    product = make_product(tenant)
    parsed = TachkaPartParser().parse_html(SAMPLE_HTML, brand='BREMBO', article='P50136')

    ProductEnrichmentService.save_parsed_part(
        tenant=tenant,
        product=product,
        parsed=parsed,
    )

    product.refresh_from_db()
    assert product.price == Decimal('1234.00')
    assert product.stock_qty == 7
    assert product.warehouse == 'Основной'
    assert product.attributes.filter(name='Ширина').exists()
    assert product.cross_codes.filter(normalized_code='A0004206000').exists()
    assert product.fitments.filter(model='E-CLASS').exists()
    assert product.enrichment_facts.filter(name='description').exists()
    assert product.oem_numbers == ['A0004206000']


@pytest.mark.django_db
def test_save_parsed_part_merges_without_erasing_existing_enrichment():
    tenant = make_tenant('parsed-merge')
    product = make_product(tenant)
    parsed = TachkaPartParser().parse_html(SAMPLE_HTML, brand='BREMBO', article='P50136')

    ProductEnrichmentService.save_parsed_part(
        tenant=tenant,
        product=product,
        parsed=parsed,
        source_id='tachka',
    )
    ProductEnrichmentService.save_parsed_part(
        tenant=tenant,
        product=product,
        parsed=ParsedPart(brand='BREMBO', article='P50136'),
        source_id='future_source',
    )

    product.refresh_from_db()
    assert product.attributes.filter(source_id='tachka', name='Ширина').exists()
    assert product.cross_codes.filter(normalized_code='A0004206000').exists()
    assert product.fitments.filter(model='E-CLASS').exists()
    assert product.oem_numbers == ['A0004206000']


@pytest.mark.django_db
def test_run_parse_job_enriches_existing_tenant_product(monkeypatch):
    tenant = make_tenant('job-run')
    product = make_product(tenant)
    job = ProductEnrichmentService.create_parse_job(
        tenant=tenant,
        product=product,
        brand='BREMBO',
        article='P50136',
        normalized_article='P50136',
    )

    class FakeParser:
        def fetch(self, brand, article):
            return SAMPLE_HTML, 'https://tachka.ru/example'

        def parse_html(self, html, brand, article, source_url=''):
            return TachkaPartParser().parse_html(html, brand, article, source_url)

    monkeypatch.setattr('apps.products.services.get_part_parser', lambda source_id: FakeParser())

    result = ProductEnrichmentService.run_parse_job(job.pk)

    job.refresh_from_db()
    product.refresh_from_db()
    assert result['status'] == 'success'
    assert job.status == 'success'
    assert job.product == product
    assert job.source_url == 'https://tachka.ru/example'
    assert job.parsed_data['normalized_article'] == 'P50136'
    assert product.price == Decimal('1234.00')
    assert product.fitments.filter(model='E-CLASS').exists()
    assert result['image_urls'] == ['https://tachka.ru/images/p50136.jpg']


def test_parse_task_queues_enrichment_images():
    from apps.products.tasks import parse_single_part

    result = {
        'job_id': 1,
        'product_id': 10,
        'status': 'success',
        'source_id': 'tachka',
        'image_urls': ['https://tachka.ru/images/p50136.jpg'],
    }

    with patch(
        'apps.products.tasks.ProductEnrichmentService.run_parse_job',
        return_value=result,
    ), patch('apps.products.tasks.download_enrichment_images.delay') as delay:
        assert parse_single_part.run(1) == result

    delay.assert_called_once_with(
        10,
        ['https://tachka.ru/images/p50136.jpg'],
        'tachka',
    )
