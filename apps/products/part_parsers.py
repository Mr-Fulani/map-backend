import json
import re
from dataclasses import dataclass, field

import httpx
from django.utils.text import slugify
from selectolax.parser import HTMLParser

from apps.products.enrichment import normalize_part_code
from apps.products.models import ProductCrossCode


DEFAULT_PART_SOURCE = 'tachka'
PART_SOURCE_CONFIGS = {
    'tachka': {
        'label': 'Tachka.ru',
        'default_pause_seconds': 60,
        'min_pause_seconds': 10,
        'batch_size': 20,
        'priority': 100,
    },
}

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


@dataclass
class ParsedCrossCode:
    manufacturer: str
    code: str
    code_type: str = ProductCrossCode.CodeType.UNKNOWN


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

    def build_url(self, brand: str, article: str) -> str:
        brand_slug = slugify(brand).lower() or brand.strip().lower()
        return f'{self.base_url}/{brand_slug}/{normalize_part_code(article)}'

    def fetch(self, brand: str, article: str) -> tuple[str, str]:
        url = self.build_url(brand, article)
        response = httpx.get(
            url,
            timeout=20,
            follow_redirects=True,
            headers={'User-Agent': 'MAP enrichment bot (+https://map.local)'},
        )
        if response.status_code == 404:
            raise PartNotFound(f'Part not found: {url}')
        response.raise_for_status()
        return response.text, str(response.url)

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


def get_part_parser(source_id: str):
    if source_id == TachkaPartParser.source_id:
        return TachkaPartParser()
    raise ValueError(f'Unknown part parser source: {source_id}')


def get_part_source_config(source_id: str) -> dict:
    if source_id not in PART_SOURCE_CONFIGS:
        raise ValueError(f'Unknown part parser source: {source_id}')
    return PART_SOURCE_CONFIGS[source_id]
