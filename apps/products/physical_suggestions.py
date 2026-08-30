"""Strict, reviewable physical facts derived from catalogue attributes."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping

from django.db import transaction
from django.utils.timezone import now

from apps.products.models import (
    Product,
    ProductPhysicalProfile,
    ProductPhysicalSuggestion,
    ReviewStatus,
)
from apps.products.source_policy import PART_SOURCE_POLICIES


_LABEL_ALIASES = {
    ProductPhysicalSuggestion.Field.BARCODE: {
        'ean', 'gtin', 'штрих код', 'ean штрих код', 'номер ean штрих код',
        'номер штрих кода', 'barcode',
    },
    ProductPhysicalSuggestion.Field.LENGTH_MM: {
        'длина упаковки', 'длина упаковки мм', 'длина упаковки см',
        'package length', 'packaging length',
    },
    ProductPhysicalSuggestion.Field.WIDTH_MM: {
        'ширина упаковки', 'ширина упаковки мм', 'ширина упаковки см',
        'package width', 'packaging width',
    },
    ProductPhysicalSuggestion.Field.HEIGHT_MM: {
        'высота упаковки', 'высота упаковки мм', 'высота упаковки см',
        'package height', 'packaging height',
    },
    ProductPhysicalSuggestion.Field.WEIGHT_G: {
        'вес', 'масса', 'вес товара', 'масса товара', 'вес упаковки',
        'масса упаковки', 'weight', 'item weight', 'package weight',
        'packaging weight',
    },
}

_NUMBER_RE = re.compile(r'(?<![\d])([+-]?\d+(?:[.,]\d+)?)(?![\d])')


@dataclass(frozen=True)
class PhysicalSuggestionCandidate:
    field: str
    value: str
    raw_name: str
    raw_value: str


class SourcePhysicalValueExists(Exception):
    """A valid 1C value already owns the effective field."""


def _normalized_label(value: str) -> str:
    normalized = (value or '').lower().replace('ё', 'е').replace('\xa0', ' ')
    normalized = re.sub(r'[^0-9a-zа-я]+', ' ', normalized)
    return ' '.join(normalized.split())


def _canonical_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal('0.001')).normalize(), 'f')


def _decimal_from_text(value: str) -> Decimal | None:
    match = _NUMBER_RE.search((value or '').replace('\xa0', ' '))
    if match is None:
        return None
    try:
        parsed = Decimal(match.group(1).replace(',', '.'))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _dimension_mm(raw_name: str, raw_value: str) -> Decimal | None:
    parsed = _decimal_from_text(raw_value)
    if parsed is None:
        return None
    unit_text = f'{raw_value} {raw_name}'.lower().replace('ё', 'е')
    if re.search(r'(?<![a-zа-я])(мм|mm)(?![a-zа-я])', unit_text):
        factor = Decimal('1')
    elif re.search(r'(?<![a-zа-я])(см|cm)(?![a-zа-я])', unit_text):
        factor = Decimal('10')
    elif re.search(r'(?<![a-zа-я])(м|m)(?![a-zа-я])', unit_text):
        factor = Decimal('1000')
    else:
        return None
    result = parsed * factor
    if result > Decimal('999999999.999'):
        return None
    return result


def _weight_g(raw_name: str, raw_value: str) -> Decimal | None:
    parsed = _decimal_from_text(raw_value)
    if parsed is None:
        return None
    unit_text = f'{raw_value} {raw_name}'.lower().replace('ё', 'е')
    if re.search(r'(?<![a-zа-я])(кг|kg|килограмм(?:а|ов)?)(?![a-zа-я])', unit_text):
        factor = Decimal('1000')
    elif re.search(r'(?<![a-zа-я])(г|гр|g|gram|grams|грамм(?:а|ов)?)(?![a-zа-я])', unit_text):
        factor = Decimal('1')
    else:
        return None
    result = parsed * factor
    if result > Decimal('999999999.999'):
        return None
    return result


def _valid_gtin(value: str) -> bool:
    if len(value) not in {8, 12, 13, 14} or not value.isdigit():
        return False
    payload = value[:-1]
    checksum = sum(
        int(digit) * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(reversed(payload))
    )
    return (10 - checksum % 10) % 10 == int(value[-1])


def _barcode(raw_value: str) -> str | None:
    candidates = re.findall(r'(?<!\d)\d{8,14}(?!\d)', raw_value or '')
    valid = list(dict.fromkeys(candidate for candidate in candidates if _valid_gtin(candidate)))
    return valid[0] if len(valid) == 1 else None


def extract_physical_suggestions(
    attributes: Mapping[str, str],
) -> dict[str, PhysicalSuggestionCandidate]:
    """Extract only explicit shipping fields; never infer from generic dimensions."""

    result: dict[str, PhysicalSuggestionCandidate] = {}
    priorities: dict[str, int] = {}
    for raw_name, raw_value in attributes.items():
        label = _normalized_label(raw_name)
        field = next(
            (candidate for candidate, aliases in _LABEL_ALIASES.items() if label in aliases),
            None,
        )
        if field is None:
            continue
        priority = (
            2
            if any(marker in label for marker in ('упаковк', 'package', 'packaging'))
            else 1 if any(marker in label for marker in ('товар', 'item')) else 0
        )
        if field in result and priorities[field] >= priority:
            continue
        if field == ProductPhysicalSuggestion.Field.BARCODE:
            value = _barcode(raw_value)
        elif field == ProductPhysicalSuggestion.Field.WEIGHT_G:
            parsed = _weight_g(raw_name, raw_value)
            value = _canonical_decimal(parsed) if parsed is not None else None
        else:
            parsed = _dimension_mm(raw_name, raw_value)
            value = _canonical_decimal(parsed) if parsed is not None else None
        if value is not None:
            result[field] = PhysicalSuggestionCandidate(
                field=field,
                value=value,
                raw_name=(raw_name or '')[:150],
                raw_value=str(raw_value or ''),
            )
            priorities[field] = priority
    return result


@transaction.atomic
def save_physical_suggestions(
    *,
    product: Product,
    attributes: Mapping[str, str],
    source_id: str,
    source_url: str,
) -> None:
    """Refresh one source without deleting its review history or writing MAP values."""

    candidates = extract_physical_suggestions(attributes)
    timestamp = now()
    current = {
        suggestion.field: suggestion
        for suggestion in ProductPhysicalSuggestion.objects.select_for_update().filter(
            tenant=product.tenant,
            product=product,
            source_id=source_id,
        )
    }
    ProductPhysicalSuggestion.objects.filter(
        tenant=product.tenant,
        product=product,
        source_id=source_id,
        is_current=True,
    ).update(is_current=False)
    policy = PART_SOURCE_POLICIES.get(source_id)
    confidence = policy.trust_score if policy is not None else 0.75
    for field, candidate in candidates.items():
        suggestion = current.get(field)
        if suggestion is None:
            ProductPhysicalSuggestion.objects.create(
                tenant=product.tenant,
                product=product,
                field=field,
                value=candidate.value,
                source_id=source_id,
                source_url=source_url,
                raw_name=candidate.raw_name,
                raw_value=candidate.raw_value,
                confidence=confidence,
                is_current=True,
                last_seen_at=timestamp,
            )
            continue
        value_changed = suggestion.value != candidate.value
        suggestion.value = candidate.value
        suggestion.source_url = source_url
        suggestion.raw_name = candidate.raw_name
        suggestion.raw_value = candidate.raw_value
        suggestion.confidence = confidence
        suggestion.is_current = True
        suggestion.last_seen_at = timestamp
        update_fields = [
            'value', 'source_url', 'raw_name', 'raw_value', 'confidence',
            'is_current', 'last_seen_at', 'updated_at',
        ]
        if value_changed:
            suggestion.review_status = ReviewStatus.PENDING
            suggestion.reviewed_at = None
            suggestion.reviewed_by = None
            update_fields.extend(['review_status', 'reviewed_at', 'reviewed_by'])
        suggestion.save(update_fields=update_fields)


def source_label(source_id: str) -> str:
    policy = PART_SOURCE_POLICIES.get(source_id)
    return policy.label if policy is not None else source_id


def physical_suggestion_presentation(suggestion: ProductPhysicalSuggestion) -> dict:
    return {
        'id': suggestion.pk,
        'field': suggestion.field,
        'value': suggestion.value,
        'source_id': suggestion.source_id,
        'source_label': source_label(suggestion.source_id),
        'source_url': suggestion.source_url,
        'raw_name': suggestion.raw_name,
        'raw_value': suggestion.raw_value,
        'confidence': suggestion.confidence,
        'review_status': suggestion.review_status,
        'last_seen_at': suggestion.last_seen_at,
    }


@transaction.atomic
def review_physical_suggestion(
    *,
    tenant,
    product_id: int,
    suggestion_id: int,
    action: str,
    actor,
) -> Product:
    product = Product.objects.select_for_update().get(pk=product_id, tenant=tenant)
    suggestion = ProductPhysicalSuggestion.objects.select_for_update().get(
        pk=suggestion_id,
        tenant=tenant,
        product=product,
        is_current=True,
    )
    if action == 'reject':
        suggestion.review_status = ReviewStatus.REJECTED
        suggestion.reviewed_at = now()
        suggestion.reviewed_by = actor
        suggestion.save(update_fields=[
            'review_status', 'reviewed_at', 'reviewed_by', 'updated_at',
        ])
        return product
    if action != 'approve':
        raise ValueError('bad_action')

    profile, _created = ProductPhysicalProfile.objects.get_or_create(
        tenant=tenant,
        product=product,
    )
    source_value = getattr(profile, f'source_{suggestion.field}')
    if source_value not in (None, ''):
        raise SourcePhysicalValueExists
    map_field = f'map_{suggestion.field}'
    value = (
        suggestion.value
        if suggestion.field == ProductPhysicalSuggestion.Field.BARCODE
        else Decimal(suggestion.value)
    )
    setattr(profile, map_field, value)
    provenance = dict(profile.map_provenance or {})
    provenance[suggestion.field] = {
        'suggestion_id': suggestion.pk,
        'source_id': suggestion.source_id,
        'source_label': source_label(suggestion.source_id),
        'source_url': suggestion.source_url,
        'raw_name': suggestion.raw_name,
        'raw_value': suggestion.raw_value,
        'accepted_at': now().isoformat(),
    }
    profile.map_provenance = provenance
    profile.save(update_fields=[map_field, 'map_provenance', 'updated_at'])
    suggestion.review_status = ReviewStatus.APPROVED
    suggestion.reviewed_at = now()
    suggestion.reviewed_by = actor
    suggestion.save(update_fields=[
        'review_status', 'reviewed_at', 'reviewed_by', 'updated_at',
    ])
    return product
