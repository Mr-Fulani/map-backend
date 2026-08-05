import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.products.models import (
    GlobalPart, GlobalPartFitment, GlobalPartRelation, Product, ProductCrossCode,
)
from apps.products.part_fetchers import FetchedPage
from apps.products.part_parsers import (
    EuroautoPartParser, ParsedFitment, ParsedPart, PartNotFound, RosskoPartParser,
    TachkaPartParser, parse_fitment_line,
)
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

TACHKA_GROUPED_FITMENTS_DESCRIPTION = (
    'Кросс коды MERCEDES-BENZ - A0004212512 '
    'Подходит для следующих модификаций: '
    'MERCEDES-BENZ C-CLASS (W205) 01.2014-01.2018 '
    'C 180 (205.040, 205.140) 156 л.с '
    'C-CLASS T-Model (S205) 05.2018-2023 C 220 d (205.214) 194 л.с'
)
TACHKA_GROUPED_FITMENTS_SCHEMA = json.dumps({
    '@context': 'https://schema.org',
    '@type': 'Product',
    'brand': {'@type': 'Brand', 'name': 'Brembo'},
    'name': 'Brembo 09.D526.13',
    'description': TACHKA_GROUPED_FITMENTS_DESCRIPTION,
}, ensure_ascii=False)

TACHKA_GROUPED_FITMENTS_HTML = f"""
<html>
  <head>
    <script type="application/ld+json">
      {TACHKA_GROUPED_FITMENTS_SCHEMA}
    </script>
  </head>
  <body>
    <h1>Brembo 09.D526.13 Тормозной диск</h1>
    <section class="product-description">
      <h2>Подходит для следующих модификаций:</h2>
      <ul>
        <h3>MERCEDES-BENZ</h3>
        <li>C-CLASS (W205) <span>01.2014-01.2018</span>
          <strong>C 180 (205.040, 205.140)</strong> 156 л.с</li>
        <li>C-CLASS T-Model (S205) <span>05.2018-2023</span>
          <strong>C 220 d (205.214)</strong> 194 л.с</li>
      </ul>
    </section>
  </body>
</html>
"""

TACHKA_JSON_LD_ONLY_DESCRIPTION = (
    'Кросс коды MERCEDES-BENZ - A0004212512 '
    'Подходит для следующих модификаций: '
    'MERCEDES-BENZ C-CLASS (W205) 01.2014-01.2018 '
    'C 180 (205.040, 205.140) 156 л.с '
    'E-CLASS (W213) 01.2016-2023 E 200 (213.042) 184 л.с'
)
TACHKA_JSON_LD_ONLY_SCHEMA = json.dumps({
    '@context': 'https://schema.org',
    '@type': 'Product',
    'brand': {'@type': 'Brand', 'name': 'Brembo'},
    'name': 'Brembo 09.D526.13',
    'description': TACHKA_JSON_LD_ONLY_DESCRIPTION,
}, ensure_ascii=False)

TACHKA_JSON_LD_ONLY_FITMENTS_HTML = f"""
<html>
  <head>
    <script type="application/ld+json">
      {TACHKA_JSON_LD_ONLY_SCHEMA}
    </script>
  </head>
  <body><h1>Brembo 09.D526.13 Тормозной диск</h1></body>
</html>
"""

# Ответ JSON-API smart-search-suggest: поиск по артикулу возвращает товар вместе
# с брендом и канонической ссылкой даже без бренда в запросе.
SUGGEST_JSON = (
    '{"query":"P50136","parsed":{},"results":null,"products":['
    '{"sku":"P 50 136","brand_name":"Brembo","price":1234,"in_stock":true,'
    '"url":"brembo/P50136"}]}'
)


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


def test_tachka_parser_extracts_grouped_fitments_with_manufacturer():
    parsed = TachkaPartParser().parse_html(
        TACHKA_GROUPED_FITMENTS_HTML,
        brand='BREMBO',
        article='09D52613',
    )

    assert len(parsed.fitments) == 2
    assert [(item.make, item.model, item.generation) for item in parsed.fitments] == [
        ('MERCEDES-BENZ', 'C-CLASS', 'W205'),
        ('MERCEDES-BENZ', 'C-CLASS T-Model', 'S205'),
    ]
    assert parsed.fitments[0].engine_code == '205.040, 205.140'
    assert parsed.fitments[1].modification == 'C 220 d'


def test_tachka_parser_extracts_fitments_from_flat_json_ld_fallback():
    parsed = TachkaPartParser().parse_html(
        TACHKA_JSON_LD_ONLY_FITMENTS_HTML,
        brand='BREMBO',
        article='09D52613',
    )

    assert len(parsed.fitments) == 2
    assert [(item.make, item.model, item.generation) for item in parsed.fitments] == [
        ('MERCEDES-BENZ', 'C-CLASS', 'W205'),
        ('MERCEDES-BENZ', 'E-CLASS', 'W213'),
    ]


def test_parse_fitment_line_keeps_uncertain_data_reviewable():
    fitment = parse_fitment_line(
        'E-CLASS (W213) 01.2016-2023 E 220 d 4-matic (213.005) 194 л.с'
    )

    assert fitment.model == 'E-CLASS'
    assert fitment.date_from == '01.2016'
    assert fitment.date_to == '2023'
    assert fitment.needs_review is False


def test_tachka_direct_fetch_requires_brand():
    """Без бренда прямой URL не строится — сразу PartNotFound (а не tachka.ru//article)."""
    class FailFetcher:
        def fetch(self, url):
            raise AssertionError(f'fetch не должен вызываться без бренда: {url}')

    with pytest.raises(PartNotFound):
        TachkaPartParser(fetcher=FailFetcher()).fetch('', 'P50136')


def test_tachka_fetch_search_resolves_via_smart_suggest_and_recovers_brand():
    """Поиск по артикулу без бренда: smart-search-suggest → карточка товара + бренд."""
    class FakeFetcher:
        def __init__(self):
            self.calls = []

        def fetch(self, url):
            self.calls.append(url)
            if 'smart-search-suggest' in url:
                return FetchedPage(html=SUGGEST_JSON, url=url, status_code=200)
            return FetchedPage(html=SAMPLE_HTML, url='https://tachka.ru/brembo/P50136', status_code=200)

    parser = TachkaPartParser(fetcher=FakeFetcher())
    html, source_url = parser.fetch_search('P 50 136')

    assert html == SAMPLE_HTML
    assert source_url == 'https://tachka.ru/brembo/P50136'
    assert parser.fetcher.calls[0].startswith('https://tachka.ru/shop/api/smart-search-suggest')

    # Бренд восстановлен из suggest и подставляется в parse_search_html.
    parsed = parser.parse_search_html(html, brand='', article='P50136', source_url=source_url)
    assert parsed.brand == 'BREMBO'


def test_tachka_match_product_disambiguates_by_name_hint():
    """Один артикул у разных брендов: выбираем товар по названию (hint)."""
    data = {'products': [
        {'sku': 'OC90', 'brand_name': 'Mahle', 'url': 'mahle/OC90',
         'title': 'Масляный фильтр Mahle', 'in_stock': True},
        {'sku': 'OC90', 'brand_name': 'AM Point', 'url': 'am-point/OC90',
         'title': 'Воздушный фильтр AM Point', 'in_stock': True},
    ]}
    parser = TachkaPartParser(fetcher=object())
    chosen = parser._match_product(data, 'OC90', hint='Фильтр воздушный для двигателя')
    assert chosen['brand_name'] == 'AM Point'


def test_tachka_fetch_search_not_found_when_sku_mismatch():
    class FakeFetcher:
        def fetch(self, url):
            other = (
                '{"products":[{"sku":"OTHER999","brand_name":"X","url":"x/OTHER999",'
                '"in_stock":true}]}'
            )
            return FetchedPage(html=other, url=url, status_code=200)

    with pytest.raises(PartNotFound):
        TachkaPartParser(fetcher=FakeFetcher()).fetch_search('P50136')


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
def test_run_parse_job_falls_back_to_search_and_backfills_brand(monkeypatch):
    """Прямой fetch 404 → fetch_search возвращает карточку товара; бренд бэкфилится."""
    tenant = make_tenant('job-search-fallback')
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='',
        name='Колодки P50136',
        price=Decimal('9997.00'),
        stock_qty=1,
    )
    job = ProductEnrichmentService.create_parse_job(
        tenant=tenant,
        product=product,
        brand='',
        article='P50136',
        normalized_article='P50136',
    )

    class FakeParser:
        def fetch(self, brand, article):
            raise PartNotFound('Tachka requires brand for direct fetch')

        def fetch_search(self, article, hint=''):
            return SAMPLE_HTML, 'https://tachka.ru/brembo/P50136'

        def parse_search_html(self, html, brand, article, source_url=''):
            # Бренд восстановлен из suggest до этого вызова.
            return TachkaPartParser().parse_html(html, brand or 'BREMBO', article, source_url)

    monkeypatch.setattr('apps.products.services.get_part_parser', lambda source_id: FakeParser())

    result = ProductEnrichmentService.run_parse_job(job.pk)

    job.refresh_from_db()
    product.refresh_from_db()
    assert result['status'] == 'success'
    assert job.status == 'success'
    assert job.source_url == 'https://tachka.ru/brembo/P50136'
    assert product.cross_codes.filter(normalized_code='A0004206000').exists()
    assert product.brand == 'BREMBO'
    assert product.brand_ref.normalized_name == 'BREMBO'
    assert product.brand_resolution_status == Product.BrandResolutionStatus.CATALOG
    assert product.brand_confidence == pytest.approx(0.85)
    assert product.brand_source_id == 'tachka'
    assert product.brand_needs_review is False


@pytest.mark.django_db
def test_catalog_brand_conflict_is_reviewable_and_does_not_overwrite_source_brand():
    tenant = make_tenant('catalog-brand-conflict')
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='SOURCE-BRAND',
        brand_resolution_status=Product.BrandResolutionStatus.SOURCE,
        brand_confidence=1.0,
        brand_source_id='csv',
        name='Колодки P50136',
        price=Decimal('1000.00'),
        stock_qty=1,
    )
    parsed = ParsedPart(
        brand='CATALOG-BRAND',
        article='P50136',
        fitments=[ParsedFitment(
            make='MERCEDES-BENZ', model='E-CLASS', generation='W213', confidence=0.95,
        )],
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, parsed, source_id='tachka')

    product.refresh_from_db()
    assert product.brand == 'SOURCE-BRAND'
    assert product.brand_resolution_status == Product.BrandResolutionStatus.AMBIGUOUS
    assert product.brand_needs_review is True
    assert product.brand_confidence == 0.5
    assert product.fitments.get(model='E-CLASS').needs_review is True
    assert not GlobalPart.objects.filter(normalized_article='P50136').exists()


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


ROSSKO_PRODUCT_HTML = """
<html>
<body>
  <div itemtype="https://schema.org/Product" itemscope>
    <meta itemprop="name" content="P 50 136 • Brembo Колодки тормозные дисковые задние" />
    <link itemprop="image" href="https://imgs.rossko.ru/46/8C/NSII0016446647/1.jpg" />
    <link itemprop="image" href="https://imgs.rossko.ru/46/8C/NSII0016446647/2.jpg" />
    <h1>P 50 136 Brembo Колодки тормозные дисковые задние</h1>
  </div>
  <div class="card__tab-content-item" data-tab-id="features" data-role="card.details.tab.content.item">
    <div class="features">
      <div class="feature-item">
        <div class="feature-item-label"><span>OEM</span></div>
        <div class="feature-item-value">Mercedes 0004206000
Mercedes A0004206100</div>
      </div>
      <div class="feature-item">
        <div class="feature-item-label"><span>Толщина [мм]</span></div>
        <div class="feature-item-value">16 мм</div>
      </div>
      <div class="feature-item">
        <div class="feature-item-label"><span>Ширина [мм]</span></div>
        <div class="feature-item-value">114.25</div>
      </div>
      <div class="feature-item">
        <div class="feature-item-label"><span>WVA номер</span></div>
        <div class="feature-item-value">22437, 22438</div>
      </div>
      <div class="feature-item">
        <div class="feature-item-label"><span>для артикула №</span></div>
        <div class="feature-item-value">P 50 136</div>
      </div>
    </div>
  </div>
  <div class="card__tab-content-item" data-tab-id="applicability" data-role="card.details.tab.content.item">
    <div class="car-applicability">
      <div class="cars" data-role="popup.body">
        <div class="car" data-role="applicability.car" data-manufacturer="MERCEDES-BENZ" data-model="E-CLASS">
          <div class="car-engines">
            <ul>
              <li>E 200 (213.042)</li>
              <li>E 220 d 4-matic (213.005)</li>
            </ul>
          </div>
        </div>
        <div class="car" data-role="applicability.car" data-manufacturer="MERCEDES-BENZ" data-model="CLS">
          <div class="car-engines">
            <ul>
              <li>CLS 220 d (257.314)</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""

ROSSKO_SEARCH_HTML = """
<html>
<body>
  <div class="data">
    <a href="/card/brembo-p-50-136-nsii0016446647/?source=searchList&amp;ref=part_number"
       class="brand-oe" data-role="product.href">
      <span class="oe">P 50 136</span>
      <span class="brand">Brembo</span>
      <span class="name">Колодки тормозные дисковые задние</span>
    </a>
  </div>
</body>
</html>
"""

ROSSKO_ALTERNATIVE_APPLICABILITY_HTML = """
<html>
<body>
  <h1>09.D526.13 Brembo Тормозной диск</h1>
  <div data-tab-id="applicability">
    <div class="car">
      <h3 class="car__manufacturer">MERCEDES-BENZ</h3>
      <h4 class="car__model">C-CLASS (W205)</h4>
      <ul class="car-engines">
        <li>C 180 (205.040)</li>
        <li>C 200 (205.042)</li>
      </ul>
    </div>
  </div>
</body>
</html>
"""

EUROAUTO_PRODUCT_HTML = """
<html>
<head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "8940-289 Metaco Фонарь задний наружный левый",
    "description": "8940-289 Metaco Фонарь задний наружный левый HYUNDAI SOLARIS (2017>) #6148741",
    "url": "https://euroauto.ru/part/new/6148741/",
    "manufacturer": "Metaco",
    "brand": {"@type": "Brand", "name": "Metaco"},
    "mpn": "8940-289",
    "image": "https://file.euroauto.ru/v2/file/parts/new/6148741/1.jpg"
  }
  </script>
</head>
<body>
  <h1>Фонарь задний наружный левый Metaco 8940-289</h1>
  <table class="part-parameters-table">
    <tr><th>№ производителя</th><td>8940-289</td></tr>
    <tr><th>Производитель</th><td>Metaco</td></tr>
    <tr><th>Применимость</th><td>2 авто</td></tr>
    <tr><th>Примечание</th><td>HYUNDAI SOLARIS (2017&gt;)</td></tr>
    <tr><th>Страна происхождения</th><td>Китай</td></tr>
  </table>
  <span class="part-applicability">
    <a href="/catalog/zadnie-fonari-1058/brand-hyundai/model-solaris/modification-solaris_2017-2022/">
      Hyundai Solaris 2017-2022
    </a>
    <a href="/catalog/zadnie-fonari-1058/brand-solaris/model-hs/modification-hs_2024-/">
      Solaris HS 2024&gt;
    </a>
  </span>
  <img src="https://file.euroauto.ru/v2/file/parts/new/6148741/2.jpg?thumbnail=308x244">
  <img src="https://file.euroauto.ru/v2/file/parts/new/9999999/1.jpg?thumbnail=116x88">
  <img src="https://file.euroauto.ru/v2/file/parts/used/1/2/3/4/1.jpg">
  <div class="slider-analog-card new-analog">
    <div class="slider-analog-card-content-brand">Hyundai-Kia</div>
    <div class="slider-analog-card-content-num">92401-H5000</div>
  </div>
  <div class="slider-analog-card new-analog">
    <div class="slider-analog-card-content-brand">SAT</div>
    <div class="slider-analog-card-content-num">ST-221-19S7L</div>
  </div>
  <div class="slider-analog-card new-analog">
    <div class="slider-analog-card-content-brand">SAT</div>
    <div class="slider-analog-card-content-num">ST-221-19S7L</div>
  </div>
</body>
</html>
"""

EUROAUTO_SEARCH_PAYLOAD = json.dumps({
    '_map_request': {
        'article': '8940-289',
        'hint': 'Фонарь задний наружный левый Kia Solaris',
    },
    '_map_product_images': [
        'https://file.euroauto.ru/v2/file/parts/new/6148741/1.jpg',
        'https://file.euroauto.ru/v2/file/parts/new/6148741/2.jpg',
    ],
    'results': [{
        'url': 'https://rostov-na-donu.euroauto.ru/firms/metaco/8940289',
        'title': '8940-289 Metaco Фонарь задний наружный левый',
        'content': (
            'Фонарь задний наружный левый Metaco 8940-289. '
            'Metaco HYUNDAI SOLARIS (2017>). '
            'Лучший аналог · SAT · ST-221-19S7L'
        ),
        'score': 0.91,
    }],
    'images': [{
        'url': 'https://file.euroauto.ru/v2/file/parts/new/6148741/1.jpg?thumbnail=308x244',
        'description': 'A rear left tail light.',
    }],
}, ensure_ascii=False)


def test_rossko_parser_extracts_features_from_html():
    parsed = RosskoPartParser().parse_html(
        ROSSKO_PRODUCT_HTML,
        brand='BREMBO',
        article='P 50 136',
        source_url='https://rossko.ru/card/brembo-p-50-136-nsii0016446647/',
    )

    assert parsed.normalized_article == 'P50136'
    assert parsed.brand == 'BREMBO'
    assert 'P 50 136' in parsed.title
    assert parsed.attributes['Толщина [мм]'] == '16 мм'
    assert parsed.attributes['Ширина [мм]'] == '114.25'
    assert parsed.attributes['WVA номер'] == '22437, 22438'
    assert 'для артикула №' not in parsed.attributes


def test_rossko_parser_extracts_oem_codes():
    parsed = RosskoPartParser().parse_html(
        ROSSKO_PRODUCT_HTML,
        brand='BREMBO',
        article='P50136',
    )

    oem_codes = [c for c in parsed.cross_codes if c.code_type == ProductCrossCode.CodeType.OEM]
    assert len(oem_codes) == 2
    assert oem_codes[0].manufacturer == 'Mercedes'
    assert oem_codes[0].code == '0004206000'
    assert oem_codes[1].code == 'A0004206100'


def test_rossko_parser_extracts_fitments():
    parsed = RosskoPartParser().parse_html(
        ROSSKO_PRODUCT_HTML,
        brand='BREMBO',
        article='P50136',
    )

    assert len(parsed.fitments) == 3
    e_class = [f for f in parsed.fitments if f.model == 'E-CLASS']
    assert len(e_class) == 2
    assert e_class[0].make == 'MERCEDES-BENZ'
    assert e_class[0].engine_code == '213.042'
    assert e_class[1].engine_code == '213.005'
    cls_fitments = [f for f in parsed.fitments if f.model == 'CLS']
    assert cls_fitments[0].engine_code == '257.314'


def test_rossko_parser_extracts_fitments_from_heading_fallback():
    parsed = RosskoPartParser().parse_html(
        ROSSKO_ALTERNATIVE_APPLICABILITY_HTML,
        brand='BREMBO',
        article='09D52613',
    )

    assert len(parsed.fitments) == 2
    assert all(item.make == 'MERCEDES-BENZ' for item in parsed.fitments)
    assert all(item.model == 'C-CLASS' for item in parsed.fitments)
    assert all(item.generation == 'W205' for item in parsed.fitments)
    assert [item.engine_code for item in parsed.fitments] == ['205.040', '205.042']


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('source_id', 'html'),
    [
        ('tachka', TACHKA_GROUPED_FITMENTS_HTML),
        ('rossko', ROSSKO_ALTERNATIVE_APPLICABILITY_HTML),
    ],
)
def test_source_fitments_are_saved_to_platform_knowledge_graph(source_id, html):
    tenant = make_tenant(f'{source_id}-fitment-learning')
    product = Product.objects.create(
        tenant=tenant,
        article='09D52613',
        brand='BREMBO',
        name='BREMBO 09D52613',
        price=Decimal('1234.00'),
        stock_qty=1,
    )
    parser = TachkaPartParser() if source_id == 'tachka' else RosskoPartParser()
    parsed = parser.parse_html(
        html,
        brand='BREMBO',
        article='09D52613',
        source_url=f'https://{source_id}.example/09D52613',
    )

    ProductEnrichmentService.save_parsed_part(
        tenant,
        product,
        parsed,
        source_id=source_id,
    )

    assert product.fitments.filter(
        source_id=source_id,
        make='MERCEDES-BENZ',
        model='C-CLASS',
        generation='W205',
    ).exists()
    assert GlobalPartFitment.objects.filter(
        part__normalized_brand='BREMBO',
        part__normalized_article='09D52613',
        source_id=source_id,
        make='MERCEDES-BENZ',
        model='C-CLASS',
        generation='W205',
        needs_review=False,
    ).exists()


def test_rossko_parser_extracts_images():
    parsed = RosskoPartParser().parse_html(
        ROSSKO_PRODUCT_HTML,
        brand='BREMBO',
        article='P50136',
    )

    assert 'https://imgs.rossko.ru/46/8C/NSII0016446647/1.jpg' in parsed.image_urls
    assert 'https://imgs.rossko.ru/46/8C/NSII0016446647/2.jpg' in parsed.image_urls


def test_rossko_parser_find_product_url_from_search():
    parser = RosskoPartParser()
    url = parser._extract_product_url(ROSSKO_SEARCH_HTML, 'P50136')

    assert url == 'https://rossko.ru/card/brembo-p-50-136-nsii0016446647/'


def test_rossko_parser_uses_injected_fetcher_without_network():
    calls = []

    class FakeFetcher:
        def fetch(self, url):
            calls.append(url)
            if 'single/search' in url:
                return FetchedPage(html=ROSSKO_SEARCH_HTML, url=url, status_code=200)
            return FetchedPage(
                html=ROSSKO_PRODUCT_HTML,
                url='https://rossko.ru/card/brembo-p-50-136-nsii0016446647/',
                status_code=200,
            )

    html, source_url = RosskoPartParser(fetcher=FakeFetcher()).fetch_search('P50136')

    assert len(calls) == 2
    assert 'single/search' in calls[0]
    assert 'brembo-p-50-136' in calls[1]
    assert html == ROSSKO_PRODUCT_HTML
    assert 'rossko.ru' in source_url


def test_euroauto_parser_extracts_product_fitments_analogues_and_only_own_images():
    parsed = EuroautoPartParser().parse_html(
        EUROAUTO_PRODUCT_HTML,
        brand='',
        article='8940289',
        source_url='https://euroauto.ru/part/new/6148741/',
    )

    assert parsed.brand == 'METACO'
    assert parsed.normalized_article == '8940289'
    assert parsed.attributes['Страна происхождения'] == 'Китай'
    assert [(item.make, item.model, item.date_from, item.date_to) for item in parsed.fitments] == [
        ('Hyundai', 'Solaris', '2017', '2022'),
        ('Solaris', 'HS', '2024', ''),
    ]
    assert [(item.brand, item.article) for item in parsed.related_parts] == [
        ('Hyundai-Kia', '92401-H5000'),
        ('SAT', 'ST-221-19S7L'),
    ]
    assert parsed.image_urls == [
        'https://file.euroauto.ru/v2/file/parts/new/6148741/1.jpg',
        'https://file.euroauto.ru/v2/file/parts/new/6148741/2.jpg',
    ]
    assert 'HYUNDAI SOLARIS' in parsed.description_facts['catalog_description']


def test_euroauto_parser_extracts_indexed_search_payload_without_source_brand():
    parsed = EuroautoPartParser(fetcher=object()).parse_search_html(
        EUROAUTO_SEARCH_PAYLOAD,
        brand='',
        article='8940-289',
        source_url='https://euroauto.ru/search/?q=8940-289',
    )

    assert parsed.brand == 'METACO'
    assert parsed.title == 'Фонарь задний наружный левый Metaco 8940-289'
    assert [(item.make, item.model, item.date_from) for item in parsed.fitments] == [
        ('HYUNDAI', 'SOLARIS', '2017'),
    ]
    assert [(item.brand, item.article) for item in parsed.related_parts] == [
        ('SAT', 'ST-221-19S7L'),
    ]
    assert len(parsed.image_urls) == 2
    assert '/parts/new/6148741/' in parsed.image_urls[0]
    assert '/firms/metaco/8940289' in parsed.source_url


def test_euroauto_parser_prefers_result_with_fitment_over_sparse_firm_page():
    payload = json.loads(EUROAUTO_SEARCH_PAYLOAD)
    payload['results'].insert(0, {
        'url': 'https://euroauto.ru/firms/metaco/8940289',
        'title': '8940-289 Metaco Фонарь задний наружный левый',
        'content': '',
        'score': 0.99,
    })
    payload['results'][1]['url'] = (
        'https://euroauto.ru/catalog/zadnie-fonari/proizvoditel-metaco'
    )
    payload['results'][1]['score'] = 0.75

    parsed = EuroautoPartParser(fetcher=object()).parse_search_html(
        json.dumps(payload, ensure_ascii=False),
        brand='METACO',
        article='8940-289',
    )

    assert [(item.make, item.model, item.date_from) for item in parsed.fitments] == [
        ('HYUNDAI', 'SOLARIS', '2017'),
    ]
    assert '/catalog/' in parsed.source_url


def test_euroauto_search_uses_injected_fetcher_and_passes_product_hint():
    calls = []

    class FakeFetcher:
        def fetch(self, url):
            calls.append(url)
            return FetchedPage(
                html=EUROAUTO_SEARCH_PAYLOAD,
                url='https://rostov-na-donu.euroauto.ru/firms/metaco/8940289',
                status_code=200,
            )

    parser = EuroautoPartParser(fetcher=FakeFetcher())
    html, source_url = parser.fetch_search(
        '8940-289', hint='Фонарь задний наружный левый',
    )

    assert html == EUROAUTO_SEARCH_PAYLOAD
    assert source_url.endswith('/firms/metaco/8940289')
    assert 'q=8940-289' in calls[0]
    assert 'hint=%D0%A4%D0%BE%D0%BD%D0%B0%D1%80%D1%8C' in calls[0]


@pytest.mark.django_db
def test_euroauto_fitments_and_analogues_are_learned_by_platform_graph():
    tenant = make_tenant('euroauto-learning')
    product = Product.objects.create(
        tenant=tenant,
        article='8940-289',
        brand='METACO',
        name='Фонарь задний наружный левый Metaco 8940-289',
        price=Decimal('4253.00'),
        stock_qty=1,
    )
    parsed = EuroautoPartParser().parse_html(
        EUROAUTO_PRODUCT_HTML,
        brand='METACO',
        article='8940-289',
        source_url='https://euroauto.ru/part/new/6148741/',
    )

    ProductEnrichmentService.save_parsed_part(
        tenant, product, parsed, source_id='euroauto',
    )

    assert GlobalPartFitment.objects.filter(
        part__normalized_brand='METACO',
        part__normalized_article='8940289',
        source_id='euroauto',
        make='Hyundai',
        model='Solaris',
        needs_review=False,
    ).exists()
    assert GlobalPartRelation.objects.filter(
        source_part__normalized_brand='METACO',
        source_part__normalized_article='8940289',
        target_part__normalized_brand='HYUNDAIKIA',
        target_part__normalized_article='92401H5000',
        source_id='euroauto',
        relation_type=GlobalPartRelation.RelationType.ANALOGUE,
        needs_review=False,
    ).exists()
