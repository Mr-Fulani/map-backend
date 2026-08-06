import re
from collections.abc import Iterable


_NAME_ALIASES = {
    'wva номер': 'WVA',
    'wva-номер': 'WVA',
}

_BARCODE_NAMES = {
    'ean',
    'ean/штрих-код',
    'номер ean/штрих-код',
    'штрих-код',
}


def normalize_attribute_text(name: str, value: str) -> tuple[str, str]:
    """Fix known catalogue wording without changing the underlying fact."""
    clean_name = ' '.join(str(name or '').split()).strip(' :')
    clean_value = ' '.join(str(value or '').split()).strip()
    clean_name = _NAME_ALIASES.get(clean_name.casefold(), clean_name)

    # Some Russian catalogue mirrors mistranslate brake caliper as "satellite".
    clean_value = re.sub(
        r'винтами\s+тормозных\s+сателлитов',
        'болтами тормозного суппорта',
        clean_value,
        flags=re.IGNORECASE,
    )
    clean_value = re.sub(
        r'с\s+прижимной\s+пластиной',
        'с противоскрипной пластиной',
        clean_value,
        flags=re.IGNORECASE,
    )
    if clean_name.casefold() == 'комплектность' and ', с ' in clean_value.casefold():
        clean_value = re.sub(
            r'^без\s+аксессуаров\s*,\s*',
            '',
            clean_value,
            flags=re.IGNORECASE,
        )
    return clean_name, clean_value


def presented_attributes(
    attributes: Iterable,
    *,
    for_ai: bool = False,
) -> list[tuple[object, str, str]]:
    """Return clean buyer-facing attributes and hide semantic duplicates."""
    prepared = []
    for item in attributes:
        name, value = normalize_attribute_text(item.name, item.value)
        if not name or not value:
            continue
        if for_ai and name.casefold() in _BARCODE_NAMES:
            continue
        prepared.append((item, name, value))

    wva_values = {
        _normalized_value(value)
        for _item, name, value in prepared
        if name.casefold() == 'wva'
    }
    result = []
    seen = set()
    for item, name, value in prepared:
        # Tachka may return the same WVA identifiers again as "trade numbers".
        if (
            name.casefold() == 'торговые номера'
            and _normalized_value(value) in wva_values
        ):
            continue
        identity = (name.casefold(), _normalized_value(value))
        if identity in seen:
            continue
        seen.add(identity)
        result.append((item, name, value))
    return result


def _normalized_value(value: str) -> str:
    return ''.join(character for character in value.casefold() if character.isalnum())
