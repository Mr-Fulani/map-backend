import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

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
FITMENT_RECORD_RE = re.compile(
    r'(?P<model>[A-ZА-ЯЁa-zа-яё0-9][A-ZА-ЯЁa-zа-яё0-9 .+&/\'_-]{0,140}?)\s*'
    r'\((?P<generation>[A-ZА-ЯЁ0-9][A-ZА-ЯЁa-zа-яё0-9 .+&/_-]{0,40})\)\s+'
    r'(?P<date_from>\d{2}\.\d{4}|\d{4})\s*[-–]\s*'
    r'(?P<date_to>\d{2}\.\d{4}|\d{4}|н\.?в\.?|н/в)?\s+'
    r'(?P<modification>.*?)\s*'
    r'\((?P<engine_code>[^()]{2,100})\)\s*'
    r'(?P<power>\d{2,4})\s*(?:л\.?\s*с\.?|hp)\b',
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
        # Бренд, восстановленный из smart-search-suggest при поиске по артикулу
        # без бренда — используется parse_search_html для бэкфилла.
        self._resolved_brand = ''

    def build_url(self, brand: str, article: str) -> str:
        brand_slug = slugify(brand).lower() or brand.strip().lower()
        return f'{self.base_url}/{brand_slug}/{normalize_part_code(article)}'

    def build_suggest_url(self, query: str) -> str:
        return f'{self.base_url}/shop/api/smart-search-suggest?query={quote(query.strip())}'

    def fetch(self, brand: str, article: str) -> tuple[str, str]:
        # Прямая карточка возможна только при известном бренде. Без бренда URL
        # вырождается в tachka.ru//article (404) — пропускаем сразу к поиску.
        if not (brand or '').strip():
            raise PartNotFound(f'Tachka requires brand for direct fetch: {article}')
        url = self.build_url(brand, article)
        page = self.fetcher.fetch(url)
        if page.status_code == 404:
            raise PartNotFound(f'Part not found: {url}')
        page.raise_for_status()
        return page.html, page.url

    def fetch_search(self, article: str, hint: str = '') -> tuple[str, str]:
        """Ищет артикул через JSON-API smart-search-suggest и грузит карточку товара.

        Работает и без бренда: API возвращает товар вместе с brand_name и
        канонической ссылкой {brand}/{article}, которую парсит parse_html.
        hint (название товара) разрешает неоднозначность, когда один артикул
        принадлежит разным брендам/деталям.
        """
        product = self._match_product(self._smart_search(article), article, hint)
        if not product:
            raise PartNotFound(f'Артикул не найден в каталоге Тачка.ру: {article}')
        self._resolved_brand = (product.get('brand_name') or '').strip()
        relative_url = (product.get('url') or '').strip().lstrip('/')
        if not relative_url:
            raise PartNotFound(f'Артикул не найден в каталоге Тачка.ру: {article}')
        product_url = f'{self.base_url}/{relative_url}'
        page = self.fetcher.fetch(product_url)
        if page.status_code == 404:
            raise PartNotFound(f'Артикул не найден в каталоге Тачка.ру: {article}')
        page.raise_for_status()
        return page.html, page.url

    def _smart_search(self, query: str) -> dict:
        page = self.fetcher.fetch(self.build_suggest_url(query))
        page.raise_for_status()
        try:
            return json.loads(page.html)
        except (ValueError, TypeError):
            return {}

    def _match_product(self, data: dict, article: str, hint: str = '') -> dict | None:
        """Выбирает товар с совпадающим артикулом.

        Один артикул бывает у разных брендов/деталей, поэтому при нескольких
        совпадениях выбираем по пересечению названия товара (hint) с title;
        при равенстве — товар в наличии.
        """
        products = (data or {}).get('products') or []
        target = normalize_part_code(article)
        matched = [
            product for product in products
            if normalize_part_code(product.get('sku') or '') == target
        ]
        if not matched:
            return None
        hint_tokens = _name_tokens(hint)
        matched.sort(
            key=lambda product: (
                len(_name_tokens(product.get('title') or '') & hint_tokens),
                bool(product.get('in_stock')),
            ),
            reverse=True,
        )
        return matched[0]

    def parse_html(self, html: str, brand: str, article: str, source_url: str = '') -> ParsedPart:
        tree = HTMLParser(html)
        raw_text = _normalize_lines(tree.body.text(separator='\n')) if tree.body else ''
        structured = self._parse_product_json_ld(tree)
        cross_codes = self._parse_cross_codes(tree, raw_text, structured.get('description', ''))
        parsed = ParsedPart(
            brand=(structured.get('brand') or brand).strip().upper(),
            article=normalize_part_code(article),
            title=(
                self._first_text(tree, ['h1', '[itemprop="name"]', '.product-title'])
                or structured.get('title', '')
            ),
            category=self._first_text(tree, ['[itemprop="category"]', '.breadcrumb li:last-child']),
            attributes=self._parse_attributes(tree),
            cross_codes=cross_codes,
            fitments=self._parse_fitments(
                tree,
                raw_text,
                structured.get('description', ''),
                known_makes=[cross.manufacturer for cross in cross_codes],
            ),
            image_urls=self._parse_image_urls(
                tree,
                structured.get('image_urls', []),
                brand=structured.get('brand') or brand,
                article=article,
            ),
            description_facts=self._parse_description_facts(tree, structured.get('description', '')),
            source_url=source_url,
            raw_text=raw_text[:20000],
        )
        if not parsed.title:
            parsed.title = f'{parsed.brand} {parsed.article}'
        return parsed

    def parse_search_html(self, html: str, brand: str, article: str, source_url: str = '') -> ParsedPart:
        """Псевдоним parse_html — fetch_search возвращает сразу страницу товара.

        brand берётся из smart-search-suggest, если у товара его не было.
        """
        return self.parse_html(html, brand or self._resolved_brand, article, source_url=source_url)

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

    def _parse_fitments(
        self,
        tree: HTMLParser,
        raw_text: str,
        structured_description: str = '',
        *,
        known_makes: list[str] | None = None,
    ) -> list[ParsedFitment]:
        """Extract Tachka applicability from its HTML groups and JSON-LD fallback.

        Tachka renders every vehicle as a structured ``h3 + li`` group, but its
        Schema.org description flattens the same list into one very long line.
        The old line-based parser therefore returned zero fitments for real
        product cards while preserving the list only as a description hint.
        """
        fitments: list[ParsedFitment] = []

        for heading in tree.css('h3'):
            group = heading.parent
            if group is None:
                continue
            group_text = _normalize_spaces(group.text(separator=' '))
            if not POWER_RE.search(group_text) or not DATE_RE.search(group_text):
                continue
            make = _normalize_spaces(heading.text(separator=' ')).strip(' :-')
            if not _looks_like_vehicle_make(make):
                continue
            for item in group.css('li'):
                line = _normalize_spaces(item.text(separator=' '))
                if not POWER_RE.search(line) or not DATE_RE.search(line):
                    continue
                fitment = parse_fitment_line(line)
                fitment.make = make
                fitments.append(fitment)

        if structured_description:
            fitments.extend(_parse_flat_fitments(
                structured_description,
                known_makes=known_makes or [],
            ))

        for line in raw_text.splitlines():
            line = _normalize_spaces(line)
            if not line or not POWER_RE.search(line) or not DATE_RE.search(line):
                continue
            fitments.append(parse_fitment_line(line))
        return _dedupe_fitments(fitments)

    def _parse_image_urls(
        self,
        tree: HTMLParser,
        structured_urls: list[str] | None = None,
        *,
        brand: str = '',
        article: str = '',
    ) -> list[str]:
        urls = [
            url for url in (structured_urls or [])
            if self._is_product_image_url(url, brand, article)
        ]
        for img in tree.css('img'):
            src = img.attributes.get('src') or img.attributes.get('data-src') or ''
            if not src or src.startswith('data:'):
                continue
            if src.startswith('//'):
                src = f'https:{src}'
            elif src.startswith('/'):
                src = f'{self.base_url}{src}'
            if src not in urls and self._is_product_image_url(src, brand, article):
                urls.append(src)
        return urls[:10]

    @staticmethod
    def _is_product_image_url(url: str, brand: str, article: str) -> bool:
        """Reject page chrome; Tachka product CDN paths contain brand/article identity."""
        normalized_url = normalize_part_code(unquote(url))
        normalized_article = normalize_part_code(article)
        normalized_brand = normalize_part_code(brand)
        lower_url = unquote(url).lower()
        if any(marker in lower_url for marker in (
            'getclicky.com', 'brandlogos/', 'placeholder', '/other/mask.',
        )):
            return False
        if lower_url.endswith(('.gif', '.svg')):
            return False
        if normalized_article and normalized_article in normalized_url:
            return True
        return bool(
            '/brand/' in lower_url
            and normalized_brand
            and normalized_brand in normalized_url
        )

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


def _parse_flat_fitments(
    text: str,
    *,
    known_makes: list[str] | None = None,
) -> list[ParsedFitment]:
    """Parse fitments flattened by JSON-LD into a single text value."""
    normalized = _normalize_spaces(text)
    description_prefix = ''
    marker = re.search(
        r'подходит\s+для\s+следующих\s+модификаций\s*:',
        normalized,
        re.IGNORECASE,
    )
    if marker:
        description_prefix = normalized[:marker.start()].strip()
        normalized = normalized[marker.end():].strip()

    description_makes = re.findall(
        r'(?:^|\s)([A-ZА-ЯЁ][A-ZА-ЯЁ0-9 /().-]{1,70}?)\s+-\s*(?=[A-ZА-ЯЁ0-9])',
        description_prefix,
    )
    makes = sorted(
        {
            normalized_make
            for make in [*(known_makes or []), *description_makes]
            if (
                (normalized_make := _normalize_spaces(make).strip(' :-'))
                and _looks_like_vehicle_make(normalized_make)
            )
        },
        key=len,
        reverse=True,
    )
    current_make = ''
    fitments: list[ParsedFitment] = []
    for match in FITMENT_RECORD_RE.finditer(normalized):
        model = _normalize_spaces(match.group('model')).strip(' ,;:-')
        make, model = _extract_make_prefix(model, makes, current_make)
        if make:
            current_make = make
        if not model:
            continue
        raw_text = _normalize_spaces(match.group(0))
        fitments.append(ParsedFitment(
            make=current_make,
            model=model,
            generation=_normalize_spaces(match.group('generation')),
            date_from=match.group('date_from'),
            date_to=match.group('date_to') or '',
            modification=_normalize_spaces(match.group('modification')),
            engine_code=_normalize_spaces(match.group('engine_code')),
            power_hp=int(match.group('power')),
            raw_text=raw_text,
            confidence=0.9,
            needs_review=False,
        ))
    return fitments


def _extract_make_prefix(
    model: str,
    known_makes: list[str],
    current_make: str = '',
) -> tuple[str, str]:
    model_folded = model.casefold()
    for make in known_makes:
        prefix = f'{make} '
        if model_folded.startswith(prefix.casefold()):
            return make, model[len(prefix):].strip()
    return current_make, model


def _split_model_generation(value: str) -> tuple[str, str]:
    value = _normalize_spaces(value)
    match = re.match(r'^(?P<model>.+?)\s*\((?P<generation>[^()]+)\)\s*$', value)
    if not match:
        return value, ''
    return match.group('model').strip(), match.group('generation').strip()


def _dedupe_fitments(fitments: list[ParsedFitment]) -> list[ParsedFitment]:
    result: list[ParsedFitment] = []
    seen = set()
    for fitment in fitments:
        key = (
            fitment.make.casefold(),
            fitment.model.casefold(),
            fitment.generation.casefold(),
            fitment.modification.casefold(),
            fitment.engine_code.casefold(),
            fitment.power_hp,
        )
        if not fitment.model or key in seen:
            continue
        seen.add(key)
        result.append(fitment)
    return result[:500]


def _normalize_spaces(value: str) -> str:
    return ' '.join((value or '').split())


def _normalize_lines(value: str) -> str:
    lines = [_normalize_spaces(line) for line in (value or '').splitlines()]
    return '\n'.join(line for line in lines if line)


def _first_node_text(node, selectors: list[str], fallback: str = '') -> str:
    fallback = _normalize_spaces(fallback)
    if fallback:
        return fallback
    for selector in selectors:
        child = node.css_first(selector)
        if child:
            value = _normalize_spaces(child.text(separator=' '))
            if value:
                return value
    return ''


def _name_tokens(text: str) -> set[str]:
    """Значимые слова названия (>2 символов) для сопоставления товаров."""
    return {token for token in re.findall(r'[0-9a-zа-яё]+', (text or '').lower()) if len(token) > 2}


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


def _looks_like_vehicle_make(value: str) -> bool:
    value = _normalize_spaces(value).strip(' :-')
    return bool(
        _looks_like_manufacturer(value)
        and len(value.split()) <= 5
        and not DATE_RE.search(value)
        and not POWER_RE.search(value)
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

    def fetch(self, brand: str, article: str) -> tuple[str, str]:
        """Rossko не поддерживает прямой запрос по brand/article — только поиск.

        Поднимает PartNotFound чтобы вызывающий код перешёл к fetch_search.
        """
        raise PartNotFound(f'Rossko does not support direct fetch for {brand} {article}')

    def fetch_search(self, article: str, hint: str = '') -> tuple[str, str]:
        """Ищет артикул, затем загружает страницу первого совпавшего товара."""
        search_url = self.build_search_url(article)
        search_page = self.fetcher.fetch(search_url)
        search_page.raise_for_status()

        if self._is_bot_challenge(search_page.html):
            raise PartNotFound('Каталог Росско временно недоступен (защита от ботов)')

        product_url = self._extract_product_url(search_page.html, article)
        if not product_url:
            raise PartNotFound(f'Артикул не найден в каталоге Росско: {article}')

        product_page = self.fetcher.fetch(product_url)
        if product_page.status_code == 404:
            raise PartNotFound(f'Артикул не найден в каталоге Росско: {article}')
        product_page.raise_for_status()
        return product_page.html, product_page.url

    @staticmethod
    def _is_bot_challenge(html: str) -> bool:
        """Rossko отдаёт короткую JS-заглушку с noindex вместо результатов поиска."""
        lowered = (html or '').lower()
        return 'noindex, noarchive' in lowered and 'data-role="product.href"' not in lowered

    def parse_search_html(self, html: str, brand: str, article: str, source_url: str = '') -> ParsedPart:
        """Псевдоним parse_html — fetch_search возвращает сразу страницу товара."""
        return self.parse_html(html, brand, article, source_url=source_url)

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
        """Парсит вкладку Применимость: марка, модель, поколение и модификация.

        Rossko uses data attributes on the current page, but older/cached and
        alternative responses expose the same values as headings and list
        items. Both shapes are supported so a harmless markup change does not
        silently turn applicability into an empty list.
        """
        fitments: list[ParsedFitment] = []

        appl_tab = tree.css_first('[data-tab-id="applicability"]')
        if not appl_tab:
            return fitments

        cars = appl_tab.css('[data-role="applicability.car"], .car')
        for car in cars:
            make = _first_node_text(
                car,
                [
                    '[data-role="applicability.manufacturer"]',
                    '.car-manufacturer',
                    '.car__manufacturer',
                    'h3',
                ],
                fallback=(
                    car.attributes.get('data-manufacturer')
                    or car.attributes.get('data-make')
                ),
            )
            raw_model = _first_node_text(car, [
                '[data-role="applicability.model"]',
                '.car-model',
                '.car__model',
                'h4',
            ], fallback=car.attributes.get('data-model'))
            model, generation = _split_model_generation(raw_model)
            if not make or not model:
                continue

            modifications = car.css(
                '.car-engines li, [data-role="applicability.engine"], '
                '[data-role="applicability.modification"]'
            )
            if not modifications:
                modifications = car.css('li')
            for item in modifications:
                modification = _normalize_spaces(item.text(separator=' '))
                if not modification:
                    continue
                if DATE_RE.search(modification) and POWER_RE.search(modification):
                    fitment = parse_fitment_line(modification)
                    fitment.make = make
                    if not fitment.model:
                        fitment.model = model
                        fitment.generation = generation
                    fitments.append(fitment)
                    continue
                engine_match = re.search(r'\(([^)]+)\)\s*$', modification)
                engine_code = engine_match.group(1) if engine_match else ''
                fitments.append(ParsedFitment(
                    make=make,
                    model=model,
                    generation=generation,
                    modification=modification,
                    engine_code=engine_code,
                    raw_text=f'{make} {model} {modification}',
                    confidence=0.85,
                    needs_review=False,
                ))

        # Fallback for Rossko responses that group full fitment lines under a
        # manufacturer heading but omit the data-role/data-* attributes.
        for heading in appl_tab.css('h3'):
            group = heading.parent
            if group is None:
                continue
            make = _normalize_spaces(heading.text(separator=' ')).strip(' :-')
            if not _looks_like_vehicle_make(make):
                continue
            for item in group.css('li'):
                line = _normalize_spaces(item.text(separator=' '))
                if not DATE_RE.search(line) or not POWER_RE.search(line):
                    continue
                fitment = parse_fitment_line(line)
                fitment.make = make
                fitments.append(fitment)

        return _dedupe_fitments(fitments)

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
