from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.products.models import GlobalPartRelation, Product, ProductCrossCode
from apps.products.part_fetchers import FetchedPage
from apps.products.part_parsers import ParsedPart, TachkaPartParser, parse_fitment_line
from apps.products.services import ProductEnrichmentService, ProductService
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

SEARCH_HTML = """
<html>
  <body>
    <h1>РЕЗУЛЬТАТЫ ПОИСКА: СТОЙКА (АЛЬТЕРНАТИВА 48510-80863) 485108Z460</h1>
    <h2>Результаты по артикулу 48510-80863)</h2>
    <div>
      Aмортизатор Toyota. Артикул 4851080863
      Toyota
      Артикул: 4851080863
    </div>
    <h2>Аналоги по OEM коду 48510-80863)</h2>
    <div>
      Амортизатор Miles. Артикул DG211003
      Miles
      Артикул: DG211003
    </div>
    <div>
      Амортизатор TOYOTA CAMRY 17- газ.пер.прав. (Kayaba) KYB. Артикул 3350048
      KYB
      Артикул: 3350048
    </div>
  </body>
</html>
"""


def make_tenant(slug, catalog_domain='auto_parts'):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    tenant.catalog_domain = catalog_domain
    tenant.save(update_fields=['catalog_domain'])
    from apps.products.services import ProductCategorySeedService
    ProductCategorySeedService.enable_tenant_catalog_domain(tenant, catalog_domain)
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


def test_tachka_parser_extracts_related_parts_from_search_html():
    parsed = TachkaPartParser().parse_search_html(
        SEARCH_HTML,
        brand='TOYOTA-LEXUS',
        article='485108Z460',
        source_url='https://tachka.ru/poisk?search=485108Z460',
    )

    assert parsed.normalized_article == '485108Z460'
    assert [part.article for part in parsed.related_parts] == [
        '4851080863',
        'DG211003',
        '3350048',
    ]
    assert parsed.related_parts[0].brand == 'Toyota'
    assert parsed.related_parts[0].relation_type == GlobalPartRelation.RelationType.OEM
    assert parsed.related_parts[1].relation_type == GlobalPartRelation.RelationType.ANALOGUE
    assert parsed.cross_codes[0].code == '4851080863'


def test_tachka_parser_uses_injected_fetcher_without_network():
    class FakeFetcher:
        def fetch(self, url):
            return FetchedPage(
                html=SAMPLE_HTML,
                url='https://tachka.ru/final/P50136',
                status_code=200,
            )

    html, source_url = TachkaPartParser(fetcher=FakeFetcher()).fetch('BREMBO', 'P50136')

    assert html == SAMPLE_HTML
    assert source_url == 'https://tachka.ru/final/P50136'


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


@pytest.mark.django_db
def test_run_parse_job_applies_known_knowledge_before_external_fetch(monkeypatch):
    tenant_a = make_tenant('job-kg-owner')
    product_a = make_product(tenant_a)
    parsed = TachkaPartParser().parse_html(SAMPLE_HTML, brand='BREMBO', article='P50136')
    ProductEnrichmentService.save_parsed_part(tenant_a, product_a, parsed)

    tenant_b = make_tenant('job-kg-consumer')
    product_b = make_product(tenant_b)
    job = ProductEnrichmentService.create_parse_job(
        tenant=tenant_b,
        product=product_b,
        brand='BREMBO',
        article='P50136',
        normalized_article='P50136',
    )

    fetch_called = False

    class FakeParser:
        def fetch(self, brand, article):
            nonlocal fetch_called
            fetch_called = True
            return SAMPLE_HTML, 'https://tachka.ru/example'

    monkeypatch.setattr('apps.products.services.get_part_parser', lambda source_id: FakeParser())

    result = ProductEnrichmentService.run_parse_job(job.pk)

    job.refresh_from_db()
    product_b.refresh_from_db()
    assert fetch_called is False
    assert result['status'] == 'success'
    assert result['fitments_count'] == 1
    assert job.parsed_data['applied_from'] == 'knowledge_graph'
    assert product_b.fitments.filter(model='E-CLASS').exists()


@pytest.mark.django_db
def test_schedule_ai_generation_enriches_auto_part_without_fitments(
    django_capture_on_commit_callbacks,
):
    tenant = make_tenant('ai-enrich-before-generate')
    product = make_product(tenant)

    with patch('apps.products.tasks.parse_single_part_then_generate_description.delay') as parse_delay:
        with patch('apps.ai_agent.tasks.generate_description_task.delay') as ai_delay:
            with django_capture_on_commit_callbacks(execute=True):
                result = ProductService.schedule_ai_generation(product, tenant)

    assert result['mode'] == 'enrich_then_generate'
    assert result['job_id'] is not None
    assert tenant.product_parse_jobs.filter(product=product).count() == 1
    parse_delay.assert_called_once_with(result['job_id'])
    ai_delay.assert_not_called()


@pytest.mark.django_db
def test_schedule_ai_generation_uses_plain_ai_when_fitments_are_already_trusted(
    django_capture_on_commit_callbacks,
):
    tenant = make_tenant('ai-generate-with-fitments')
    product = make_product(tenant)
    ProductEnrichmentService.create_fitment(
        tenant=tenant,
        product=product,
        make='MERCEDES-BENZ',
        model='E-CLASS',
        generation='W213',
        confidence=0.95,
    )

    with patch('apps.products.tasks.parse_single_part_then_generate_description.delay') as parse_delay:
        with patch('apps.ai_agent.tasks.generate_description_task.delay') as ai_delay:
            with django_capture_on_commit_callbacks(execute=True):
                result = ProductService.schedule_ai_generation(product, tenant)

    assert result['mode'] == 'generate'
    assert result['job_id'] is None
    assert tenant.product_parse_jobs.count() == 0
    parse_delay.assert_not_called()
    ai_delay.assert_called_once_with(product.pk)


@pytest.mark.django_db
def test_run_parse_job_falls_back_to_search_results(monkeypatch):
    tenant = make_tenant('job-search-fallback')
    product = Product.objects.create(
        tenant=tenant,
        article='485108Z460',
        brand='TOYOTA-LEXUS',
        name='СТОЙКА 485108Z460',
        price=Decimal('9997.00'),
        stock_qty=1,
    )
    job = ProductEnrichmentService.create_parse_job(
        tenant=tenant,
        product=product,
        brand='TOYOTA-LEXUS',
        article='485108Z460',
        normalized_article='485108Z460',
    )

    class FakeParser:
        def fetch(self, brand, article):
            from apps.products.part_parsers import PartNotFound
            raise PartNotFound('direct page not found')

        def fetch_search(self, article):
            return SEARCH_HTML, 'https://tachka.ru/poisk?search=485108Z460'

        def parse_search_html(self, html, brand, article, source_url=''):
            return TachkaPartParser().parse_search_html(html, brand, article, source_url)

    monkeypatch.setattr('apps.products.services.get_part_parser', lambda source_id: FakeParser())

    result = ProductEnrichmentService.run_parse_job(job.pk)

    job.refresh_from_db()
    product.refresh_from_db()
    assert result['status'] == 'need_review'
    assert job.status == 'need_review'
    assert job.source_url == 'https://tachka.ru/poisk?search=485108Z460'
    assert product.cross_codes.filter(normalized_code='4851080863').exists()


def test_parse_task_saves_enrichment_images_before_finishing():
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
    ), patch('apps.products.tasks._save_enrichment_images') as save_images:
        assert parse_single_part.run(1) == result

    save_images.assert_called_once_with(result)


def test_clean_enrichment_image_urls_filters_service_images_and_tachka_variants():
    from apps.products.tasks import _clean_enrichment_image_urls

    urls = [
        'https://img.tachka.ru/a=/trim:top-left:50/fit-in/1500x1875/brand/brembo/brembo-P50136-fWiOnIu.jpg',
        'https://img.tachka.ru/b=/trim:top-left:50/fit-in/420x800/brand/brembo/brembo-P50136-fWiOnIu.jpg',
        'https://img.tachka.ru/c=/trim:top-left:50/fit-in/2000x0/'
        'filters:watermark(other/mask.png,0,0,0)/brand/brembo/brembo-P50136-fWiOnIu.jpg',
        'https://img.tachka.ru/logo=/trim:top-left:50/fit-in/200x0/brandlogos/brembo.png',
        'https://in.getclicky.com/100846186ns.gif',
        'https://img.tachka.ru/d=/trim:top-left:50/fit-in/420x800/brand/brembo/brembo-P50136-other.jpg',
    ]

    assert _clean_enrichment_image_urls(urls) == [
        urls[0],
        urls[5],
    ]
