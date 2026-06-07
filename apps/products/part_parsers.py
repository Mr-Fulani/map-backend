import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from django.utils.text import slugify
from selectolax.parser import HTMLParser

from apps.products.enrichment import normalize_part_code
from apps.products.part_fetchers import get_part_fetcher
from apps.products.models import GlobalPartRelation, ProductCrossCode


POWER_RE = re.compile(r'(?P<power>\d{2,4})\s*(?:л\.?\s*с|hp)', re.IGNORECASE)
DATE_RE = re.compile(
    r'(?P<date_from>\d{2}\.\d{4}|\d{4})\s*[-–]\s*(?P<date_to>\d{2}\.\d{4}|\d{4}|н\.в\.|н/в)?',
    re.IGNORECASE,
)
PARENS_RE = re.compile(r'\(([^()]+)\)')
CROSS_PAIR_RE = re.compile(
    r'(?P<manufacturer>[A-ZА-ЯЁ][A-ZА-ЯЁ0-9 /().-]{1,70}?)\s+-\s*'
    r'(?P<code>[A-Z0-9][A-Z0-9 ./-]{2,40}?)(?=\s+[A-ZА-ЯЁ][A-ZА-ЯЁ0-9 /().-]{1,70}?\s+-|$)'
)
PRODUCT_RESULT_RE = re.compile(
    r'(?P<title>.+?)\s+Артикул:?\s*(?P<article>[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9 ./-]{2,40})',
    re.IGNORECASE,
)


@dataclass
class ParsedCrossCode:
    manufacturer: str
    code: str
    code_type: str = ProductCrossCode.CodeType.UNKNOWN


@dataclass
class ParsedRelatedPart:
    brand: str
    article: str
    title: str = ''
    relation_type: str = GlobalPartRelation.RelationType.UNKNOWN
    raw_text: str = ''
    confidence: float = 0.8
    needs_review: bool = False


@dataclass
class ParsedFitment:
    make: str = ''
    model: str = ''
    generation: str = ''
    date_from: str = ''
    date_to: str = ''
    modification: str = ''
    engine_code: str = ''
    power_hp: int | None = None
    raw_text: str = ''
    confidence: float = 1.0
    needs_review: bool = False


@dataclass
class ParsedPart:
    brand: str
    article: str
    title: str = ''
    category: str = ''
    attributes: dict[str, str] = field(default_factory=dict)
    cross_codes: list[ParsedCrossCode] = field(default_factory=list)
    related_parts: list[ParsedRelatedPart] = field(default_factory=list)
    fitments: list[ParsedFitment] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    description_facts: dict[str, str] = field(default_factory=dict)
    source_url: str = ''
    raw_text: str = ''

    @property
    def normalized_article(self) -> str:
        return normalize_part_code(self.article)

    def to_dict(self) -> dict:
        return {
            'brand': self.brand,
            'article': self.article,
            'normalized_article': self.normalized_article,
            'title': self.title,
            'category': self.category,
            'attributes': self.attributes,
            'cross_codes': [cross.__dict__ for cross in self.cross_codes],
            'related_parts': [related.__dict__ for related in self.related_parts],
            'fitments': [fitment.__dict__ for fitment in self.fitments],
            'image_urls': self.image_urls,
            'description_facts': self.description_facts,
            'source_url': self.source_url,
        }


class PartNotFound(Exception):
    """Источник явно сообщил, что товар не найден."""


class TachkaPartParser:
    """HTML parser для enrichment-данных каталога tachka.ru."""

    source_id = 'tachka'
    base_url = 'https://tachka.ru'

    def __init__(self, fetcher=None):
        self.fetcher = fetcher or get_part_fetcher(self.source_id)

    def build_url(self, brand: str, article: str) -> str:
        brand_slug = slugify(brand).lower() or brand.strip().lower()
        return f'{self.base_url}/{brand_slug}/{normalize_part_code(article)}'

    def build_search_urls(self, article: str) -> list[str]:
        encoded = quote(article.strip())
        normalized = quote(normalize_part_code(article))
        return [
            f'{self.base_url}/poisk?search={encoded}',
            f'{self.base_url}/poisk?q={encoded}',
            f'{self.base_url}/poisk?query={encoded}',
            f'{self.base_url}/poisk?article={normalized}',
        ]

    def fetch(self, brand: str, article: str) -> tuple[str, str]:
        url = self.build_url(brand, article)
        page = self.fetcher.fetch(url)
        if page.status_code == 404:
            raise PartNotFound(f'Part not found: {url}')
        page.raise_for_status()
        return page.html, page.url

    def fetch_search(self, article: str) -> tuple[str, str]:
        for url in self.build_search_urls(article):
            page = self.fetcher.fetch(url)
            if page.status_code == 404:
                continue
            page.raise_for_status()
            if self.search_html_has_results(page.html):
                return page.html, page.url
        raise PartNotFound(f'Part not found in tachka search: {article}')

    def parse_html(self, html: str, brand: str, article: str, source_url: str = '') -> ParsedPart:
        tree = HTMLParser(html)
        raw_text = _normalize_lines(tree.body.text(separator='\n')) if tree.body else ''
        structured = self._parse_product_json_ld(tree)
        parsed = ParsedPart(
            brand=(structured.get('brand') or brand).strip().upper(),
            article=normalize_part_code(article),
            title=(
                self._first_text(tree, ['h1', '[itemprop="name"]', '.product-title'])
                or structured.get('title', '')
            ),
            category=self._first_text(tree, ['[itemprop="category"]', '.breadcrumb li:last-child']),
            attributes=self._parse_attributes(tree),
            cross_codes=self._parse_cross_codes(tree, raw_text, structured.get('description', '')),
            fitments=self._parse_fitments(raw_text),
            image_urls=self._parse_image_urls(tree, structured.get('image_urls', [])),
            description_facts=self._parse_description_facts(tree, structured.get('description', '')),
            source_url=source_url,
            raw_text=raw_text[:20000],
        )
        if not parsed.title:
            parsed.title = f'{parsed.brand} {parsed.article}'
        return parsed

    def search_html_has_results(self, html: str) -> bool:
        tree = HTMLParser(html)
        raw_text = _normalize_lines(tree.body.text(separator='\n')) if tree.body else ''
        return bool(
            raw_text
            and 'товары не найдены' not in raw_text.lower()
            and (
                'результаты поиска' in raw_text.lower()
                or 'аналоги по oem' in raw_text.lower()
                or 'артикул:' in raw_text.lower()
            )
        )

    def parse_search_html(self, html: str, brand: str, article: str, source_url: str = '') -> ParsedPart:
        tree = HTMLParser(html)
        raw_text = _normalize_lines(tree.body.text(separator='\n')) if tree.body else ''
        normalized_article = normalize_part_code(article)
        parsed = ParsedPart(
            brand=brand.strip().upper(),
            article=normalized_article,
            title=f'{brand} {article}'.strip(),
            source_url=source_url,
            raw_text=raw_text[:20000],
            related_parts=self._parse_related_parts_from_search(raw_text, normalized_article),
            description_facts=self._parse_search_description_facts(raw_text),
        )
        parsed.cross_codes = [
            ParsedCrossCode(
                manufacturer=related.brand,
                code=related.article,
                code_type=_relation_to_code_type(related.relation_type),
            )
            for related in parsed.related_parts
            if related.relation_type in [
                GlobalPartRelation.RelationType.OEM,
                GlobalPartRelation.RelationType.CROSS,
                GlobalPartRelation.RelationType.ANALOGUE,
                GlobalPartRelation.RelationType.REPLACEMENT,
            ]
        ][:50]
        return parsed

    def _parse_related_parts_from_search(
        self, raw_text: str, normalized_article: str,
    ) -> list[ParsedRelatedPart]:
        related_parts = []
        current_relation_type = GlobalPartRelation.RelationType.UNKNOWN
        lines = raw_text.splitlines()
        for line in lines:
            lowered = line.lower()
            if 'аналоги по oem' in lowered or 'аналоги' in lowered:
                current_relation_type = GlobalPartRelation.RelationType.ANALOGUE
                continue
            if 'результаты по артикулу' in lowered:
                current_relation_type = GlobalPartRelation.RelationType.OEM
                continue

            match = PRODUCT_RESULT_RE.search(line)
            if not match:
                continue
            found_article = _normalize_spaces(match.group('article')).strip(' .,-)')
            normalized_found_article = normalize_part_code(found_article)
            if not normalized_found_article or normalized_found_article == normalized_article:
                continue

            title = _normalize_spaces(match.group('title'))
            brand = _extract_brand_from_search_title(title)
            if not brand:
                continue
            needs_review = current_relation_type == GlobalPartRelation.RelationType.UNKNOWN
            related_parts.append(ParsedRelatedPart(
                brand=brand,
                article=found_article,
                title=title,
                relation_type=current_relation_type,
                raw_text=line,
                confidence=0.7 if needs_review else 0.9,
                needs_review=needs_review,
            ))
        return _dedupe_related_parts(related_parts)

    def _parse_search_description_facts(self, raw_text: str) -> dict[str, str]:
        facts = {}
        for line in raw_text.splitlines():
            normalized = _normalize_spaces(line)
            if not normalized:
                continue
            lowered = normalized.lower()
            if any(marker in lowered for marker in ['toyota camry', 'hyundai', 'kia', 'mercedes']):
                facts['search_hint'] = normalized[:3000]
                break
        return facts

    def _first_text(self, tree: HTMLParser, selectors: list[str]) -> str:
        for selector in selectors:
            node = tree.css_first(selector)
            if node:
                text = _normalize_spaces(node.text(separator=' '))
                if text:
                    return text
        return ''

    def _parse_attributes(self, tree: HTMLParser) -> dict[str, str]:
        attributes = {}
        for row in tree.css('tr'):
            cells = [_normalize_spaces(cell.text(separator=' ')) for cell in row.css('th,td')]
            if len(cells) >= 2 and cells[0] and cells[1]:
                attributes[cells[0].rstrip(':')] = cells[1]
        for item in tree.css('[data-attribute-name]'):
            name = _normalize_spaces(item.attributes.get('data-attribute-name', '')).rstrip(':')
            value = _normalize_spaces(item.text(separator=' '))
            if name and value:
                attributes[name] = value
        return attributes

    def _parse_cross_codes(
        self, tree: HTMLParser, raw_text: str, structured_description: str = '',
    ) -> list[ParsedCrossCode]:
        codes = []
        for row in tree.css('tr'):
            cells = [_normalize_spaces(cell.text(separator=' ')) for cell in row.css('th,td')]
            if (
                len(cells) >= 2
                and _looks_like_cross_label(cells[0])
                and _looks_like_cross_code(cells[1])
            ):
                codes.append(ParsedCrossCode(
                    manufacturer=cells[0].rstrip(':'),
                    code=cells[1],
                    code_type=_guess_code_type(cells[0]),
                ))
        for text in [structured_description, *_extract_cross_sections(raw_text)]:
            codes.extend(_parse_cross_pairs(text))
        return _dedupe_cross_codes(codes)

    def _parse_fitments(self, raw_text: str) -> list[ParsedFitment]:
        fitments = []
        for line in raw_text.splitlines():
            line = _normalize_spaces(line)
            if not line or not POWER_RE.search(line) or not DATE_RE.search(line):
                continue
            fitments.append(parse_fitment_line(line))
        return fitments[:500]

    def _parse_image_urls(self, tree: HTMLParser, structured_urls: list[str] | None = None) -> list[str]:
        urls = list(structured_urls or [])
        for img in tree.css('img'):
            src = img.attributes.get('src') or img.attributes.get('data-src') or ''
            if not src or src.startswith('data:'):
                continue
            if src.startswith('//'):
                src = f'https:{src}'
            elif src.startswith('/'):
                src = f'{self.base_url}{src}'
            if src not in urls:
                urls.append(src)
        return urls[:10]

    def _parse_description_facts(self, tree: HTMLParser, structured_description: str = '') -> dict[str, str]:
        facts = {}
        if structured_description:
            facts['description'] = structured_description[:3000]
            return facts
        for selector in ['.description', '[itemprop="description"]', '.product-description']:
            node = tree.css_first(selector)
            if node:
                text = _normalize_spaces(node.text(separator=' '))
                if text:
                    facts['description'] = text[:3000]
                    break
        return facts

    def _parse_product_json_ld(self, tree: HTMLParser) -> dict:
        for script in tree.css('script[type="application/ld+json"]'):
            raw = script.text(strip=True)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            product = _find_product_schema(payload)
            if not product:
                continue
            brand = product.get('brand') or {}
            image = product.get('image') or []
            if isinstance(brand, dict):
                brand_name = brand.get('name', '')
            else:
                brand_name = str(brand)
            if isinstance(image, str):
                image_urls = [image]
            else:
                image_urls = [url for url in image if isinstance(url, str)]
            return {
                'brand': brand_name,
                'title': product.get('name', ''),
                'description': _normalize_spaces(product.get('description', '')),
                'image_urls': image_urls,
            }
        return {}


def parse_fitment_line(line: str) -> ParsedFitment:
    power_match = POWER_RE.search(line)
    date_match = DATE_RE.search(line)
    parens = PARENS_RE.findall(line)
    generation = parens[0] if parens else ''
    engine_code = parens[-1] if parens else ''

    model_part = line[:date_match.start()].strip() if date_match else line
    model = model_part.split('(', 1)[0].strip() if '(' in model_part else model_part

    modification = ''
    if date_match:
        end = power_match.start() if power_match else len(line)
        modification = PARENS_RE.sub('', line[date_match.end():end]).strip()

    return ParsedFitment(
        model=model,
        generation=generation,
        date_from=date_match.group('date_from') if date_match else '',
        date_to=date_match.group('date_to') if date_match and date_match.group('date_to') else '',
        modification=modification,
        engine_code=engine_code,
        power_hp=int(power_match.group('power')) if power_match else None,
        raw_text=line,
        confidence=0.9 if model and date_match and power_match else 0.5,
        needs_review=not (model and date_match and power_match),
    )


def _normalize_spaces(value: str) -> str:
    return ' '.join((value or '').split())


def _normalize_lines(value: str) -> str:
    lines = [_normalize_spaces(line) for line in (value or '').splitlines()]
    return '\n'.join(line for line in lines if line)


def _looks_like_cross_code(value: str) -> bool:
    if any(char in value for char in '{};=<>'):
        return False
    normalized = normalize_part_code(value)
    return bool(
        3 <= len(normalized) <= 40
        and any(char.isdigit() for char in normalized)
    )


def _looks_like_manufacturer(value: str) -> bool:
    normalized = _normalize_spaces(value).strip(' :-')
    lowered = normalized.lower()
    blocked = ('if ', 'var ', 'function', 'return', 'window', 'self.', 'this.')
    return bool(
        2 <= len(normalized) <= 80
        and not any(char in normalized for char in '{};=<>')
        and not lowered.startswith(blocked)
        and any(char.isalpha() for char in normalized)
    )


def _guess_code_type(label: str) -> str:
    label = label.lower()
    if 'oem' in label or 'oe' in label or 'ориг' in label:
        return ProductCrossCode.CodeType.OEM
    if 'cross' in label or 'аналог' in label or 'замен' in label:
        return ProductCrossCode.CodeType.CROSS
    return ProductCrossCode.CodeType.UNKNOWN


def _relation_to_code_type(relation_type: str) -> str:
    if relation_type == GlobalPartRelation.RelationType.OEM:
        return ProductCrossCode.CodeType.OEM
    if relation_type == GlobalPartRelation.RelationType.TRADE:
        return ProductCrossCode.CodeType.TRADE
    if relation_type in [
        GlobalPartRelation.RelationType.CROSS,
        GlobalPartRelation.RelationType.ANALOGUE,
        GlobalPartRelation.RelationType.REPLACEMENT,
    ]:
        return ProductCrossCode.CodeType.CROSS
    return ProductCrossCode.CodeType.UNKNOWN


def _looks_like_cross_label(label: str) -> bool:
    label = label.lower()
    markers = ('oem', 'oe', 'ориг', 'cross', 'аналог', 'замен', 'кросс')
    return any(marker in label for marker in markers)


def _dedupe_cross_codes(codes: list[ParsedCrossCode]) -> list[ParsedCrossCode]:
    seen = set()
    result = []
    for code in codes:
        key = (code.manufacturer, normalize_part_code(code.code), code.code_type)
        if key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result[:200]


def _dedupe_related_parts(parts: list[ParsedRelatedPart]) -> list[ParsedRelatedPart]:
    seen = set()
    result = []
    for part in parts:
        key = (part.brand, normalize_part_code(part.article), part.relation_type)
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return result[:100]


def _extract_brand_from_search_title(title: str) -> str:
    normalized = _normalize_spaces(title)
    if not normalized:
        return ''

    patterns = [
        r'\([A-Za-zА-Яа-яЁё0-9 -]{2,40}\)\s+([A-Za-zА-Яа-яЁё0-9 -]{2,40})\.',
        r'\b(?:Aмортизатор|Амортизатор|Стойка|Колодки|Диск|Рейка)\s+([A-Za-zА-Яа-яЁё0-9 -]{2,40})\.',
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        candidate = _normalize_spaces(match.group(match.lastindex)).strip(' .,-()')
        if _looks_like_manufacturer(candidate):
            return candidate

    chunks = normalized.split()
    for chunk in reversed(chunks):
        candidate = chunk.strip(' .,-()')
        if candidate.isupper() and _looks_like_manufacturer(candidate):
            return candidate
    return ''


def _extract_cross_sections(raw_text: str) -> list[str]:
    lines = raw_text.splitlines()
    sections = []
    for index, line in enumerate(lines):
        if 'кросс' not in line.lower():
            continue
        section = lines[index:index + 40]
        sections.append('\n'.join(section))
    return sections


def _parse_cross_pairs(text: str) -> list[ParsedCrossCode]:
    if not text:
        return []
    normalized = _normalize_spaces(
        text
        .replace('\n-', ' - ')
        .replace('-\n', ' - ')
        .replace('\n', ' ')
    )
    normalized = re.sub(r'\bКросс\s+коды\b', '', normalized, flags=re.IGNORECASE).strip()
    codes = []
    for match in CROSS_PAIR_RE.finditer(normalized):
        manufacturer = _normalize_spaces(match.group('manufacturer')).strip(' :-')
        code = _normalize_spaces(match.group('code')).strip(' .,-')
        if _looks_like_manufacturer(manufacturer) and _looks_like_cross_code(code):
            codes.append(ParsedCrossCode(
                manufacturer=manufacturer,
                code=code,
                code_type=_guess_code_type(manufacturer),
            ))
    return codes


def _find_product_schema(payload):
    if isinstance(payload, list):
        for item in payload:
            found = _find_product_schema(item)
            if found:
                return found
    if not isinstance(payload, dict):
        return None
    schema_type = payload.get('@type')
    if schema_type == 'Product' or (
        isinstance(schema_type, list) and 'Product' in schema_type
    ):
        return payload
    graph = payload.get('@graph')
    if graph:
        return _find_product_schema(graph)
    return None


class RosskoPartParser:
    """HTML parser для enrichment-данных каталога rossko.ru.

    Не поддерживает прямую карточку по brand/article — только поиск через
    /single/search/?q={article}, затем переход на страницу товара.
    Все данные в SSR HTML без авторизации.
    """

    source_id = 'rossko'
    base_url = 'https://rossko.ru'

    def __init__(self, fetcher=None):
        self.fetcher = fetcher or get_part_fetcher(self.source_id)

    def build_search_url(self, article: str) -> str:
        return f'{self.base_url}/single/search/?q={quote(normalize_part_code(article))}'

    def fetch_search(self, article: str) -> tuple[str, str]:
        """Ищет артикул, затем загружает страницу первого совпавшего товара."""
        search_url = self.build_search_url(article)
        search_page = self.fetcher.fetch(search_url)
        search_page.raise_for_status()

        product_url = self._extract_product_url(search_page.html, article)
        if not product_url:
            raise PartNotFound(f'Part not found in rossko: {article}')

        product_page = self.fetcher.fetch(product_url)
        if product_page.status_code == 404:
            raise PartNotFound(f'Rossko product page not found: {product_url}')
        product_page.raise_for_status()
        return product_page.html, product_page.url

    def parse_html(self, html: str, brand: str, article: str, source_url: str = '') -> ParsedPart:
        """Извлекает enrichment-данные из HTML страницы товара rossko.ru."""
        tree = HTMLParser(html)
        raw_text = _normalize_lines(tree.body.text(separator='\n')) if tree.body else ''

        title_node = tree.css_first('h1')
        title = _normalize_spaces(title_node.text(separator=' ')) if title_node else ''

        attributes, cross_codes = self._parse_features(tree)
        fitments = self._parse_applicability(tree)
        image_urls = self._parse_images(tree)

        parsed = ParsedPart(
            brand=brand.strip().upper(),
            article=normalize_part_code(article),
            title=title or f'{brand} {article}'.strip(),
            attributes=attributes,
            cross_codes=cross_codes,
            fitments=fitments,
            image_urls=image_urls,
            source_url=source_url,
            raw_text=raw_text[:20000],
        )
        return parsed

    def _extract_product_url(self, html: str, article: str) -> str:
        """Возвращает URL страницы товара с артикулом, совпадающим с искомым."""
        tree = HTMLParser(html)
        normalized = normalize_part_code(article)
        for link in tree.css('a[data-role="product.href"]'):
            oe_node = link.css_first('.oe')
            if oe_node and normalize_part_code(oe_node.text()) == normalized:
                href = (link.attributes.get('href') or '').split('?')[0]
                if href.startswith('/card/'):
                    return f'{self.base_url}{href}'
        return ''

    def _parse_features(self, tree: HTMLParser) -> tuple[dict[str, str], list[ParsedCrossCode]]:
        """Парсит вкладку Характеристики: атрибуты и OEM-коды."""
        attributes: dict[str, str] = {}
        cross_codes: list[ParsedCrossCode] = []

        features_tab = tree.css_first('[data-tab-id="features"]')
        if not features_tab:
            return attributes, cross_codes

        for item in features_tab.css('.feature-item'):
            label_node = item.css_first('.feature-item-label span')
            value_node = item.css_first('.feature-item-value')
            if not label_node or not value_node:
                continue

            label = _normalize_spaces(label_node.text())
            if not label or label.lower().startswith('для артикула'):
                continue

            if label.upper() == 'OEM':
                for line in value_node.text(separator='\n').splitlines():
                    line = _normalize_spaces(line)
                    if not line:
                        continue
                    parts = line.split(' ', 1)
                    if len(parts) == 2:
                        manufacturer, code = parts[0].strip(), parts[1].strip()
                        if _looks_like_cross_code(code):
                            cross_codes.append(ParsedCrossCode(
                                manufacturer=manufacturer,
                                code=code,
                                code_type=ProductCrossCode.CodeType.OEM,
                            ))
            else:
                value = _normalize_spaces(value_node.text(separator=' '))
                if value:
                    attributes[label] = value

        return attributes, _dedupe_cross_codes(cross_codes)

    def _parse_applicability(self, tree: HTMLParser) -> list[ParsedFitment]:
        """Парсит вкладку Применимость: марка, модель, модификация."""
        fitments: list[ParsedFitment] = []

        appl_tab = tree.css_first('[data-tab-id="applicability"]')
        if not appl_tab:
            return fitments

        for car in appl_tab.css('.car[data-role="applicability.car"]'):
            make = (car.attributes.get('data-manufacturer') or '').strip()
            model = (car.attributes.get('data-model') or '').strip()
            if not make or not model:
                continue

            for li in car.css('.car-engines ul li'):
                modification = _normalize_spaces(li.text())
                if not modification:
                    continue
                engine_match = re.search(r'\(([^)]+)\)\s*$', modification)
                engine_code = engine_match.group(1) if engine_match else ''
                fitments.append(ParsedFitment(
                    make=make,
                    model=model,
                    modification=modification,
                    engine_code=engine_code,
                    raw_text=f'{make} {model} {modification}',
                    confidence=0.85,
                    needs_review=False,
                ))

        return fitments[:500]

    def _parse_images(self, tree: HTMLParser) -> list[str]:
        """Извлекает URL изображений из microdata Schema.org Product."""
        urls: list[str] = []
        for link in tree.css('[itemtype*="schema.org/Product"] link[itemprop="image"]'):
            href = (link.attributes.get('href') or '').strip()
            if href and href not in urls:
                urls.append(href)
        return urls[:10]


def get_part_parser(source_id: str):
    if source_id == TachkaPartParser.source_id:
        return TachkaPartParser()
    if source_id == RosskoPartParser.source_id:
        return RosskoPartParser()
    raise ValueError(f'Unknown part parser source: {source_id}')
