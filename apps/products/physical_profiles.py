"""Validated physical product facts with explicit 1C/MAP provenance."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import cast

from django.utils.timezone import now

from apps.datasources.models import DataSourceConnection
from apps.products.models import Product, ProductPhysicalProfile, ProductPhysicalSuggestion
from apps.products.physical_suggestions import physical_suggestion_presentation


PHYSICAL_FIELD_LABELS = {
    'barcode': 'Штрихкод',
    'length_mm': 'Длина',
    'width_mm': 'Ширина',
    'height_mm': 'Высота',
    'weight_g': 'Вес',
    'vat_rate': 'НДС',
}
PHYSICAL_FIELDS = tuple(PHYSICAL_FIELD_LABELS)
VAT_RATES = frozenset({Decimal('0'), Decimal('5'), Decimal('7'), Decimal('10'), Decimal('20')})
MAX_PHYSICAL_DECIMAL = Decimal('999999999.999')

_SOURCE_SPECS = {
    'barcode': (('barcode', None), ('ean', None), ('gtin', None)),
    'length_mm': (
        ('length_mm', Decimal('1')),
        ('depth_mm', Decimal('1')),
        ('length_cm', Decimal('10')),
        ('depth_cm', Decimal('10')),
    ),
    'width_mm': (
        ('width_mm', Decimal('1')),
        ('width_cm', Decimal('10')),
    ),
    'height_mm': (
        ('height_mm', Decimal('1')),
        ('height_cm', Decimal('10')),
    ),
    'weight_g': (
        ('weight_g', Decimal('1')),
        ('weight_kg', Decimal('1000')),
    ),
    'vat_rate': (('vat_rate', None), ('vat', None)),
}
_SOURCE_KEYS = frozenset(
    key
    for specs in _SOURCE_SPECS.values()
    for key, _factor in specs
)
_SOURCE_TYPES = frozenset({
    DataSourceConnection.TYPE_1C_HTTP,
    DataSourceConnection.TYPE_1C_XML,
})


def _first_present(data: Mapping[str, object], field: str) -> tuple[object | None, Decimal | None, bool]:
    for key, factor in _SOURCE_SPECS[field]:
        if key in data:
            return data.get(key), factor, True
    return None, None, False


def _normalize_barcode(value: object) -> str:
    if isinstance(value, (bool, list, tuple, dict, set)):
        raise ValueError('Некорректный штрихкод.')
    barcode = str(value or '').strip()
    if not barcode:
        return ''
    if len(barcode) > 64 or any(ord(char) < 32 for char in barcode):
        raise ValueError('Некорректный штрихкод.')
    return barcode


def _normalize_positive_decimal(value: object, factor: Decimal | None = None) -> Decimal | None:
    if value is None or str(value).strip() == '':
        return None
    try:
        result = Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Некорректное число.') from exc
    if factor is not None:
        result *= factor
    if not result.is_finite() or result <= 0 or result > MAX_PHYSICAL_DECIMAL:
        raise ValueError('Значение должно быть положительным числом допустимого размера.')
    return result.quantize(Decimal('0.001'))


def normalize_vat_rate(value: object) -> Decimal | None:
    if value is None or str(value).strip() == '':
        return None
    raw = str(value).strip().replace(',', '.').replace('%', '')
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError('Некорректная ставка НДС.') from exc
    if result in {Decimal('0.05'), Decimal('0.07'), Decimal('0.1'), Decimal('0.2')}:
        result *= 100
    if result not in VAT_RATES:
        raise ValueError('Допустимые ставки НДС: 0%, 5%, 7%, 10% или 20%.')
    return result.quantize(Decimal('0.01'))


def _normalize_source_values(
    data: Mapping[str, object],
) -> tuple[dict[str, str | Decimal | None], dict[str, str]]:
    values: dict[str, str | Decimal | None] = {}
    errors: dict[str, str] = {}
    for field in PHYSICAL_FIELDS:
        raw, factor, present = _first_present(data, field)
        try:
            if field == 'barcode':
                values[field] = _normalize_barcode(raw) if present else ''
            elif field == 'vat_rate':
                values[field] = normalize_vat_rate(raw) if present else None
            else:
                values[field] = _normalize_positive_decimal(raw, factor) if present else None
        except ValueError as exc:
            values[field] = '' if field == 'barcode' else None
            errors[field] = str(exc)
    return values, errors


def get_product_physical_profile(product: Product) -> ProductPhysicalProfile | None:
    try:
        return product.physical_profile
    except ProductPhysicalProfile.DoesNotExist:
        return None


def _has_source_payload(data: Mapping[str, object]) -> bool:
    return any(
        key in data and data.get(key) is not None and str(data.get(key)).strip()
        for key in _SOURCE_KEYS
    )


def sync_source_physical_profile(
    product: Product,
    datasource: DataSourceConnection,
    data: Mapping[str, object],
) -> ProductPhysicalProfile | None:
    """Replace only the 1C half of the profile; MAP fallback is untouched."""

    if datasource.type not in _SOURCE_TYPES:
        return None
    profile = get_product_physical_profile(product)
    if profile is None and not _has_source_payload(data):
        return None

    source_values, source_errors = _normalize_source_values(data)
    if profile is None:
        profile = ProductPhysicalProfile(tenant=product.tenant, product=product)
    profile.source_barcode = str(source_values['barcode'])
    profile.source_length_mm = cast(Decimal | None, source_values['length_mm'])
    profile.source_width_mm = cast(Decimal | None, source_values['width_mm'])
    profile.source_height_mm = cast(Decimal | None, source_values['height_mm'])
    profile.source_weight_g = cast(Decimal | None, source_values['weight_g'])
    profile.source_vat_rate = cast(Decimal | None, source_values['vat_rate'])
    profile.source_errors = source_errors
    profile.source_updated_at = now()
    profile.save()
    return profile


def update_map_physical_profile(
    product: Product,
    values: Mapping[str, object],
) -> ProductPhysicalProfile:
    """Patch tenant-entered fallback fields without touching 1C facts."""

    profile, _created = ProductPhysicalProfile.objects.get_or_create(
        tenant=product.tenant,
        product=product,
    )
    update_fields = []
    provenance = dict(profile.map_provenance or {})
    provenance_changed = False
    for field, value in values.items():
        model_field = f'map_{field}'
        setattr(profile, model_field, value or '' if field == 'barcode' else value)
        update_fields.append(model_field)
        if field in provenance:
            provenance.pop(field)
            provenance_changed = True
    if provenance_changed:
        profile.map_provenance = provenance
        update_fields.append('map_provenance')
    if update_fields:
        profile.save(update_fields=[*update_fields, 'updated_at'])
    return profile


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), 'f')


def physical_profile_presentation(product: Product) -> dict:
    """Return effective values and provenance without creating rows on GET."""

    profile = get_product_physical_profile(product)
    facts = {}
    missing_fields = []
    source_errors = profile.source_errors if profile is not None else {}
    map_provenance = profile.map_provenance if profile is not None else {}
    for field in PHYSICAL_FIELD_LABELS:
        source_value = getattr(profile, f'source_{field}', None) if profile is not None else None
        map_value = getattr(profile, f'map_{field}', None) if profile is not None else None
        if field == 'barcode':
            source_value = source_value or ''
            map_value = map_value or ''
            source_serialized = source_value or None
            map_serialized = map_value or None
        else:
            source_serialized = _decimal_text(source_value)
            map_serialized = _decimal_text(map_value)
        if source_value not in (None, ''):
            effective_value = source_value
            effective_source = '1c'
        elif map_value not in (None, ''):
            effective_value = map_value
            effective_source = 'map'
        else:
            effective_value = None
            effective_source = 'missing'
            missing_fields.append(field)
        facts[field] = {
            'source_value': source_serialized,
            'map_value': map_serialized,
            'effective_value': (
                effective_value or None
                if field == 'barcode'
                else _decimal_text(effective_value)
            ),
            'effective_source': effective_source,
            'source_error': str(source_errors.get(field) or ''),
            'map_provenance': map_provenance.get(field),
        }
    prefetched = getattr(product, '_prefetched_objects_cache', {}).get(
        'physical_suggestions',
    )
    suggestions: list[ProductPhysicalSuggestion]
    if prefetched is None:
        suggestions = list(product.physical_suggestions.filter(
            tenant=product.tenant,
            is_current=True,
        ).order_by('field', '-confidence', 'source_id'))
    else:
        suggestions = sorted(
            (
                suggestion
                for suggestion in prefetched
                if suggestion.tenant_id == product.tenant_id and suggestion.is_current
            ),
            key=lambda suggestion: (
                suggestion.field,
                -suggestion.confidence,
                suggestion.source_id,
            ),
        )
    return {
        'facts': facts,
        'suggestions': [
            physical_suggestion_presentation(suggestion)
            for suggestion in suggestions
        ],
        'units': {'dimensions': 'mm', 'weight': 'g', 'vat': 'percent'},
        'complete': not missing_fields,
        'missing_fields': missing_fields,
        'source_updated_at': profile.source_updated_at if profile is not None else None,
        'updated_at': profile.updated_at if profile is not None else None,
    }
