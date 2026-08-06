from dataclasses import asdict, dataclass
from types import SimpleNamespace
from urllib.parse import urlparse

from apps.web_research.models import TenantWebResearchSettings


CIS_COUNTRY_CODES = ['RU', 'BY', 'KZ', 'AM', 'KG', 'UZ', 'AZ', 'MD', 'TJ']
COUNTRY_QUERY_LABELS = {
    'RU': 'Россия', 'BY': 'Беларусь', 'KZ': 'Казахстан', 'AM': 'Армения',
    'KG': 'Кыргызстан', 'UZ': 'Узбекистан', 'AZ': 'Азербайджан',
    'MD': 'Молдова', 'TJ': 'Таджикистан', 'GE': 'Грузия', 'UA': 'Украина',
    'TR': 'Турция', 'DE': 'Германия', 'PL': 'Польша', 'CZ': 'Чехия',
    'LT': 'Литва', 'LV': 'Латвия', 'EE': 'Эстония', 'CN': 'Китай',
    'KR': 'Южная Корея', 'JP': 'Япония', 'AE': 'ОАЭ', 'US': 'США',
    'GB': 'Великобритания', 'FR': 'Франция', 'IT': 'Италия', 'ES': 'Испания',
    'NL': 'Нидерланды',
}
COUNTRY_TLDS = {
    'ru': 'RU', 'рф': 'RU', 'by': 'BY', 'kz': 'KZ', 'am': 'AM', 'kg': 'KG',
    'uz': 'UZ', 'az': 'AZ', 'md': 'MD', 'tj': 'TJ', 'ua': 'UA', 'ge': 'GE',
    'tr': 'TR', 'de': 'DE', 'pl': 'PL', 'cz': 'CZ', 'lt': 'LT', 'lv': 'LV',
    'ee': 'EE', 'cn': 'CN', 'kr': 'KR', 'jp': 'JP', 'ae': 'AE', 'us': 'US',
    'uk': 'GB', 'fr': 'FR', 'it': 'IT', 'es': 'ES', 'nl': 'NL',
}


@dataclass(frozen=True)
class SearchContext:
    country_code: str = ''
    language: str = 'ru'
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    market_intent: str = 'pricing'
    strict_region: bool = True
    result_limit: int = 20

    def to_snapshot(self) -> dict:
        payload = asdict(self)
        payload['include_domains'] = list(self.include_domains)
        payload['exclude_domains'] = list(self.exclude_domains)
        return payload


def normalize_country_codes(values) -> list[str]:
    result = []
    for value in values or []:
        code = str(value or '').strip().upper()
        if len(code) == 2 and code.isalpha() and code not in result:
            result.append(code)
    return result


def normalized_domains(values) -> tuple[str, ...]:
    result = []
    for value in values or []:
        raw = str(value or '').strip().lower()
        host = (urlparse(raw if '://' in raw else f'https://{raw}').hostname or '')
        host = host.removeprefix('www.')
        if host and host not in result:
            result.append(host)
    return tuple(result)


def get_tenant_research_settings(tenant) -> TenantWebResearchSettings:
    settings, _ = TenantWebResearchSettings.objects.get_or_create(tenant=tenant)
    return settings


def country_codes_for_settings(settings: TenantWebResearchSettings) -> list[str]:
    selected = normalize_country_codes(settings.country_codes)
    if settings.region_preset == TenantWebResearchSettings.RegionPreset.RUSSIA:
        return ['RU']
    if settings.region_preset == TenantWebResearchSettings.RegionPreset.RUSSIA_CIS:
        return selected or CIS_COUNTRY_CODES
    if settings.region_preset == TenantWebResearchSettings.RegionPreset.CUSTOM:
        return selected
    return ['']


def build_search_contexts(settings: TenantWebResearchSettings, *, purpose: str) -> list[SearchContext]:
    include_domains = normalized_domains(settings.preferred_domains)
    exclude_domains = normalized_domains(settings.excluded_domains)
    country_codes = country_codes_for_settings(settings) or ['']
    per_country_limit = max(1, min(settings.result_limit, 50))
    return [
        SearchContext(
            country_code=code,
            language=settings.search_language or 'ru',
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            market_intent='pricing' if purpose in {'pricing', 'combined'} else 'enrichment',
            strict_region=settings.region_preset != TenantWebResearchSettings.RegionPreset.WORLDWIDE,
            result_limit=per_country_limit,
        )
        for code in country_codes
    ]


def search_contexts_from_snapshot(snapshot: dict, *, purpose: str) -> list[SearchContext]:
    defaults = {
        'region_preset': TenantWebResearchSettings.RegionPreset.RUSSIA,
        'country_codes': [],
        'search_language': 'ru',
        'preferred_domains': [],
        'excluded_domains': [],
        'result_limit': 30,
    }
    defaults.update(snapshot or {})
    return build_search_contexts(SimpleNamespace(**defaults), purpose=purpose)


def localize_query(query: str, context: SearchContext) -> str:
    if context.market_intent != 'pricing':
        return query
    parts = [query, 'купить цена наличие']
    country = COUNTRY_QUERY_LABELS.get(context.country_code)
    if country:
        parts.append(country)
    return ' '.join(part for part in parts if part).strip()


def infer_country_code(url: str, text: str = '') -> str:
    host = (urlparse(url).hostname or '').lower()
    suffix = host.rsplit('.', 1)[-1]
    if suffix in COUNTRY_TLDS:
        return COUNTRY_TLDS[suffix]
    lowered = text.casefold()
    if '₽' in text or ' руб' in lowered or 'россия' in lowered:
        return 'RU'
    for code, label in COUNTRY_QUERY_LABELS.items():
        if label.casefold() in lowered:
            return code
    return ''


def result_matches_context(url: str, text: str, context: SearchContext) -> bool:
    host = (urlparse(url).hostname or '').lower().removeprefix('www.')
    if any(host == domain or host.endswith(f'.{domain}') for domain in context.exclude_domains):
        return False
    if not context.strict_region or not context.country_code:
        return True
    detected = infer_country_code(url, text)
    # Unknown geography is retained as evidence, but the resulting offer will
    # require review and therefore cannot enter market aggregates.
    return not detected or detected == context.country_code
