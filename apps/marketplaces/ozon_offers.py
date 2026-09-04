import re
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.marketplaces.models import (
    MarketplaceAccount,
    OzonAccountProfile,
    OzonAttributeValueSnapshot,
    OzonCategoryAttributeSnapshot,
    OzonOfferDraft,
)
from apps.marketplaces.ozon_catalog import OzonCatalogService
from apps.marketplaces.ozon_category_policies import (
    OzonCategoryPolicyError,
    resolved_category_type_policy,
)
from apps.products.models import Product, ProductImage
from apps.products.physical_profiles import physical_profile_presentation
from apps.products.physical_suggestions import valid_gtin


class OzonOfferError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


PHYSICAL_LABELS = {
    'barcode': 'Штрихкод',
    'length_mm': 'Длина',
    'width_mm': 'Ширина',
    'height_mm': 'Высота',
    'weight_g': 'Вес',
    'vat_rate': 'НДС',
}
PHYSICAL_SOURCE_GUIDANCE = {
    'barcode': (
        'Возьмите реальный штрихкод с упаковки, из 1С или карточки поставщика. '
        'Не вводите случайный код.'
    ),
    'length_mm': (
        'Укажите длину товара в готовой упаковке: возьмите её у производителя '
        'или измерьте упаковку.'
    ),
    'width_mm': (
        'Укажите ширину товара в готовой упаковке: возьмите её у производителя '
        'или измерьте упаковку.'
    ),
    'height_mm': (
        'Укажите высоту товара в готовой упаковке: возьмите её у производителя '
        'или измерьте упаковку.'
    ),
    'weight_g': (
        'Укажите вес товара вместе с упаковкой: возьмите его у производителя '
        'или взвесьте упакованный товар.'
    ),
}

MAX_DICTIONARY_SNAPSHOTS_PER_ATTRIBUTE = 20
PRICE_QUANTUM = Decimal('0.01')
BOOLEAN_ATTRIBUTE_TYPES = frozenset({'boolean', 'bool'})
BOOLEAN_ATTRIBUTE_NAMES = frozenset({'нужен код маркировки'})
GENERIC_TYPE_TOKEN_PREFIXES = (
    'автомоб',
    'автозап',
    'аксессуар',
    'детал',
    'запчаст',
    'комплект',
    'набор',
    'проч',
    'товар',
    'универсал',
)


def _normalized_words(value: str) -> list[str]:
    return re.findall(r'[0-9a-zа-я]+', value.casefold().replace('ё', 'е'))


def _compact_text(value: str) -> str:
    return ''.join(_normalized_words(value))


def _attribute_kind(name: str) -> str:
    normalized = ' '.join(_normalized_words(name))
    if normalized == 'бренд' or normalized == 'brand':
        return 'brand'
    if normalized == 'тип' or normalized == 'тип товара':
        return 'type'
    return 'other'


def _selected_text(values: Any) -> str:
    if not isinstance(values, list) or not values or not isinstance(values[0], Mapping):
        return ''
    value = values[0].get('value')
    return value.strip() if isinstance(value, str) else ''


def _type_matches_product(type_name: str, product_text: str) -> bool:
    type_words = [
        word for word in _normalized_words(type_name)
        if len(word) >= 4
        and not any(word.startswith(prefix) for prefix in GENERIC_TYPE_TOKEN_PREFIXES)
    ]
    if not type_words:
        return True
    product_words = _normalized_words(product_text)
    return any(
        type_word == product_word
        or (
            len(type_word) >= 5
            and len(product_word) >= 5
            and type_word[:5] == product_word[:5]
        )
        for type_word in type_words
        for product_word in product_words
    )


def _semantic_quality_recommendations(
    product: Product,
    draft: OzonOfferDraft | None,
    schema: OzonCategoryAttributeSnapshot | None,
) -> list[dict[str, str]]:
    """Warn about plausible human mistakes without blocking a valid Ozon draft."""

    if draft is None or schema is None:
        return []
    selected = _selected_by_identity(draft)
    recommendations: list[dict[str, str]] = []
    selected_type = ''
    for attribute in schema.attributes:
        identity = (attribute['attribute_complex_id'], attribute['id'])
        value = _selected_text(selected.get(identity))
        if not value:
            continue
        kind = _attribute_kind(str(attribute.get('name') or ''))
        field = f'attribute:{identity[0]}:{identity[1]}'
        if kind == 'brand' and (product.brand or '').strip():
            product_brand = product.brand.strip()
            if _compact_text(value) != _compact_text(product_brand):
                recommendations.append(_issue(
                    'brand_value_mismatch',
                    field,
                    str(attribute.get('name') or 'Бренд'),
                    f'В товаре указан бренд «{product_brand}», а для Ozon — «{value}». '
                    'Это может быть другой бренд: перепроверьте значение в справочнике Ozon.',
                ))
        elif kind == 'type':
            selected_type = value

    type_name = selected_type or draft.type_name.strip()
    product_text = ' '.join(filter(None, [
        product.title_ai,
        product.name,
        product.description_ai,
    ]))
    if type_name and product_text and not _type_matches_product(type_name, product_text):
        recommendations.append(_issue(
            'type_value_mismatch',
            'category',
            'Категория и тип Ozon',
            f'Тип «{type_name}» не подтверждается названием товара. '
            'Перепроверьте категорию: MAP не блокирует отправку, потому что название '
            'у поставщика может отличаться.',
        ))
    return recommendations


def _latest_schema(
    account: MarketplaceAccount,
    category_id: int | None,
    type_id: int | None,
) -> OzonCategoryAttributeSnapshot | None:
    if category_id is None or type_id is None:
        return None
    return OzonCategoryAttributeSnapshot.objects.filter(
        account=account,
        description_category_id=category_id,
        type_id=type_id,
        language='DEFAULT',
    ).order_by('-updated_at', '-pk').first()


def _selected_by_identity(
    draft: OzonOfferDraft | None,
) -> dict[tuple[int, int], list[dict]]:
    if draft is None or not isinstance(draft.attributes, list):
        return {}
    result: dict[tuple[int, int], list[dict]] = {}
    for item in draft.attributes:
        if not isinstance(item, Mapping):
            continue
        try:
            identity = (int(item.get('complex_id', 0)), int(item['id']))
        except (KeyError, TypeError, ValueError):
            continue
        values = item.get('values')
        if isinstance(values, list):
            result[identity] = values
    return result


def _attribute_presentation(
    schema: OzonCategoryAttributeSnapshot | None,
    draft: OzonOfferDraft | None,
) -> list[dict[str, Any]]:
    if schema is None:
        return []
    selected = _selected_by_identity(draft)
    return [{
        **attribute,
        'complex_id': attribute['attribute_complex_id'],
        'selected_values': selected.get(
            (attribute['attribute_complex_id'], attribute['id']),
            [],
        ),
    } for attribute in schema.attributes]


def _issue(code: str, field: str, label: str, message: str) -> dict[str, str]:
    return {'code': code, 'field': field, 'label': label, 'message': message}


def _is_boolean_attribute(definition: Mapping[str, Any]) -> bool:
    if int(definition.get('dictionary_id') or 0) > 0:
        return False
    attribute_type = str(definition.get('type') or '').strip().casefold()
    attribute_name = str(definition.get('name') or '').strip().casefold()
    return (
        attribute_type in BOOLEAN_ATTRIBUTE_TYPES
        or attribute_name in BOOLEAN_ATTRIBUTE_NAMES
    )


def _stored_attribute_value_error(
    definition: Mapping[str, Any],
    values: Any,
) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    if int(definition.get('dictionary_id') or 0) > 0:
        for item in values:
            if not isinstance(item, Mapping):
                return 'Выберите значение из справочника Ozon.'
            value_id = item.get('dictionary_value_id')
            value = item.get('value')
            if (
                isinstance(value_id, bool)
                or not isinstance(value_id, int)
                or value_id <= 0
                or not isinstance(value, str)
                or not value.strip()
            ):
                return 'Выберите значение из справочника Ozon.'
        return None
    if _is_boolean_attribute(definition):
        if len(values) != 1 or not isinstance(values[0], Mapping):
            return 'Выберите только «Да» или «Нет».'
        value = values[0].get('value')
        value_id = values[0].get('dictionary_value_id', 0)
        if value not in {'true', 'false'} or value_id not in (0, None, ''):
            return 'Выберите только «Да» или «Нет».'
        return None
    for item in values:
        if not isinstance(item, Mapping):
            return 'Проверьте значение характеристики Ozon.'
        value = item.get('value')
        value_id = item.get('dictionary_value_id', 0)
        if not isinstance(value, str) or not value.strip() or value_id not in (0, None, ''):
            return 'Проверьте значение характеристики Ozon.'
    return None


def _autofill_presentation(draft: OzonOfferDraft | None) -> dict[str, Any]:
    raw: Mapping[str, Any] = (
        draft.autofill
        if draft is not None and isinstance(draft.autofill, Mapping)
        else {}
    )
    raw_fields = raw.get('fields')
    fields = {}
    if isinstance(raw_fields, Mapping):
        for key, value in raw_fields.items():
            if not isinstance(value, Mapping):
                continue
            presented = dict(value.items())
            if presented.get('state') in {'tenant_confirmed', 'kept_manual'}:
                presented.update({
                    'state': 'tenant_entered',
                    'source_label': 'Введено вручную',
                    'confidence': 0.0,
                    'message': (
                        'MAP сохранил значение, но не считает ручной ввод '
                        'автоматическим подтверждением достоверности.'
                    ),
                })
            fields[str(key)] = presented
    raw_recommendations = raw.get('recommendations')
    recommendations = raw_recommendations if isinstance(raw_recommendations, list) else []
    return {
        'status': str(raw.get('status') or 'not_started'),
        'updated_at': raw.get('updated_at'),
        'moderated_at': raw.get('moderated_at'),
        'applied_count': int(raw.get('applied_count') or 0),
        'preserved_count': int(raw.get('preserved_count') or 0),
        'fields': fields,
        'recommendations': [
            dict(item.items()) for item in recommendations if isinstance(item, Mapping)
        ],
    }


def _offer_pricing(
    product: Product,
    account: MarketplaceAccount,
    draft: OzonOfferDraft | None,
) -> dict[str, Any] | None:
    if (
        draft is None
        or draft.description_category_id is None
        or draft.type_id is None
    ):
        return None
    try:
        resolution = resolved_category_type_policy(
            account,
            description_category_id=draft.description_category_id,
            type_id=draft.type_id,
        )
    except OzonCategoryPolicyError:
        return None
    if resolution is None:
        return None

    policy = resolution['policy']
    base_price = Decimal(product.price).quantize(
        PRICE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    category_margin_pct = Decimal(policy['effective_margin_pct'])
    if draft.price_override is not None:
        final_price = Decimal(draft.price_override).quantize(
            PRICE_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
        margin_pct = (
            (
                (final_price / base_price - Decimal('1')) * Decimal('100')
            ).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
            if base_price > 0
            else Decimal('0')
        )
        margin_source = 'offer_price'
    else:
        margin_pct = (
            Decimal(draft.margin_pct)
            if draft.margin_pct is not None
            else category_margin_pct
        )
        final_price = (
            base_price * (Decimal('1') + margin_pct / Decimal('100'))
        ).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        margin_source = 'offer_margin' if draft.margin_pct is not None else 'category'
    return {
        'base_price': str(base_price),
        'effective_margin_pct': str(margin_pct),
        'margin_override': (
            str(draft.margin_pct) if draft.margin_pct is not None else None
        ),
        'price_override': (
            str(draft.price_override) if draft.price_override is not None else None
        ),
        'margin_source': margin_source,
        'final_price': str(final_price),
        'policy': policy,
    }


def _preflight(
    product: Product,
    account: MarketplaceAccount,
    draft: OzonOfferDraft | None,
    schema: OzonCategoryAttributeSnapshot | None,
    pricing: dict[str, Any] | None,
    physical: dict[str, Any],
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    recommendations: list[dict[str, str]] = []
    profile = OzonAccountProfile.objects.filter(account=account).first()
    if not account.is_active:
        errors.append(_issue('account_inactive', 'account', 'Аккаунт', 'Аккаунт Ozon выключен.'))
    if profile is None or profile.connection_status != OzonAccountProfile.ConnectionStatus.CONNECTED:
        errors.append(_issue(
            'account_not_ready', 'account', 'Аккаунт',
            'Сначала проверьте подключение аккаунта Ozon.',
        ))
    elif not profile.selected_warehouse_id:
        errors.append(_issue(
            'warehouse_missing', 'warehouse', 'Склад',
            'В аккаунте Ozon не выбран FBS-склад.',
        ))

    if draft is None:
        errors.append(_issue(
            'draft_missing', 'offer_id', 'Черновик Ozon',
            'Начните подготовку товара для выбранного аккаунта.',
        ))
    elif draft.description_category_id is None or draft.type_id is None:
        errors.append(_issue(
            'category_missing', 'category', 'Категория Ozon',
            'Выберите конечную категорию и тип товара Ozon.',
        ))
    else:
        tree, types = OzonCatalogService.category_types(account)
        current_type = next((item for item in types if (
            item['description_category_id'] == draft.description_category_id
            and item['type_id'] == draft.type_id
        )), None)
        if tree is None or current_type is None:
            errors.append(_issue(
                'category_outdated', 'category', 'Категория Ozon',
                'Категория отсутствует в последнем локальном снимке Ozon.',
            ))
        elif draft.tree_revision != tree.schema_hash:
            errors.append(_issue(
                'tree_revision_outdated', 'category', 'Категория Ozon',
                'Дерево Ozon обновилось — подтвердите категорию ещё раз.',
            ))

        if pricing is not None and not pricing['policy']['effective_enabled']:
            errors.append(_issue(
                'category_disabled',
                'category',
                'Категория Ozon',
                'Эта категория выключена для выбранного кабинета. '
                'Включите её во вкладке «Настройки → Категории Ozon» '
                'или выберите другой тип товара.',
            ))

        if schema is None:
            errors.append(_issue(
                'attribute_schema_missing',
                'attributes',
                'Характеристики Ozon',
                'Загрузите схему характеристик выбранной категории.',
            ))
        else:
            if draft.attribute_schema_revision != schema.schema_hash:
                errors.append(_issue(
                    'attribute_schema_outdated',
                    'attributes',
                    'Характеристики Ozon',
                    'Схема характеристик обновилась — проверьте значения.',
                ))
            selected = _selected_by_identity(draft)
            autofill_fields = (
                draft.autofill.get('fields', {})
                if isinstance(draft.autofill, Mapping)
                and isinstance(draft.autofill.get('fields'), Mapping)
                else {}
            )
            for attribute in schema.attributes:
                identity = (
                    attribute['attribute_complex_id'],
                    attribute['id'],
                )
                selected_values = selected.get(identity)
                if attribute['is_required'] and not selected_values:
                    errors.append(_issue(
                        'required_attribute_missing',
                        f'attribute:{identity[0]}:{identity[1]}',
                        attribute['name'],
                        'Заполните обязательную характеристику Ozon.',
                    ))
                    continue
                value_error = _stored_attribute_value_error(
                    attribute,
                    selected_values,
                )
                if value_error:
                    errors.append(_issue(
                        'invalid_attribute_value',
                        f'attribute:{identity[0]}:{identity[1]}',
                        attribute['name'],
                        value_error,
                    ))
                    continue
                autofill_field = autofill_fields.get(f'{identity[0]}:{identity[1]}')
                if (
                    selected_values
                    and isinstance(autofill_field, Mapping)
                    and autofill_field.get('state') == 'suggested'
                ):
                    errors.append(_issue(
                        'suggested_attribute_needs_confirmation',
                        f'attribute:{identity[0]}:{identity[1]}',
                        attribute['name'],
                        'MAP нашёл значение в обогащении. Проверьте его и сохраните '
                        'характеристики перед отправкой.',
                    ))

    recommendations.extend(_semantic_quality_recommendations(product, draft, schema))

    for field in physical['missing_fields']:
        if field == 'barcode':
            recommendations.append(_issue(
                'barcode_generated_after_import',
                'physical:barcode',
                PHYSICAL_LABELS[field],
                'Штрихкод не нужен для создания карточки. После получения Product ID '
                'MAP сможет запросить отдельный штрихкод Ozon.',
            ))
        elif field == 'vat_rate':
            recommendations.append(_issue(
                'vat_recommended',
                'physical:vat_rate',
                PHYSICAL_LABELS[field],
                'Если ставка известна из 1С или от бухгалтера, укажите её в MAP. '
                'Без ставки подготовка Ozon не блокируется.',
            ))
        else:
            errors.append(_issue(
                'physical_fact_missing', f'physical:{field}', PHYSICAL_LABELS[field],
                PHYSICAL_SOURCE_GUIDANCE[field],
            ))
    barcode = str(physical['facts']['barcode']['effective_value'] or '').strip()
    if barcode and not valid_gtin(barcode):
        errors.append(_issue(
            'barcode_invalid',
            'physical:barcode',
            PHYSICAL_LABELS['barcode'],
            'Штрихкод не прошёл проверку EAN/GTIN. Исправьте его или очистите: '
            'без кода MAP создаст карточку, а затем сможет запросить код Ozon.',
        ))
    if not (product.title_ai or product.name).strip():
        errors.append(_issue('name_missing', 'name', 'Название', 'У товара нет названия.'))
    if Decimal(product.price) <= 0:
        errors.append(_issue('price_missing', 'price', 'Цена', 'Цена должна быть больше нуля.'))
    elif pricing is not None and Decimal(pricing['final_price']) <= 0:
        errors.append(_issue(
            'offer_price_invalid',
            'price',
            'Цена Ozon',
            'После применения наценки цена Ozon должна быть больше нуля. '
            'Исправьте цену в карточке или наценку категории выбранного кабинета.',
        ))
    if not product.images.filter(status__in=(
        ProductImage.Status.AUTO_APPROVED,
        ProductImage.Status.MANUALLY_SET,
        ProductImage.Status.IMPORTED,
    )).exists():
        errors.append(_issue(
            'image_missing',
            'images',
            'Фотографии',
            'Добавьте или одобрите хотя бы одну фотографию.',
        ))
    if not (product.description_ai or '').strip():
        recommendations.append(_issue(
            'description_recommended', 'description', 'Описание',
            'Добавьте описание — карточка будет понятнее покупателю.',
        ))
    if product.stock_qty == 0:
        recommendations.append(_issue(
            'stock_zero', 'stock', 'Остаток',
            'Остаток равен нулю: после будущей публикации товар не появится в продаже.',
        ))
    return {'ready': not errors, 'errors': errors, 'recommendations': recommendations}


def offer_presentation(product: Product, account: MarketplaceAccount) -> dict[str, Any]:
    draft = OzonOfferDraft.objects.filter(
        tenant=product.tenant,
        product=product,
        account=account,
    ).first()
    schema = _latest_schema(
        account,
        draft.description_category_id if draft else None,
        draft.type_id if draft else None,
    )
    pricing = _offer_pricing(product, account, draft)
    physical_profile = physical_profile_presentation(product)
    from apps.marketplaces.ozon_publication import (
        latest_offer_operation,
        operation_presentation,
        ozon_barcode_generation_enabled_for_account,
    )
    from apps.marketplaces.ozon_rollout import ozon_product_write_enabled_for_account

    latest_operation = latest_offer_operation(draft)
    from apps.marketplaces.ozon_commerce import commerce_presentation
    common_barcode = str(
        physical_profile['facts']['barcode']['effective_value'] or '',
    ).strip()
    provider_barcodes = [
        str(value).strip()[:100]
        for value in (draft.provider_barcodes if draft is not None else [])
        if str(value).strip()
    ][:50]
    barcode_generation_enabled = ozon_barcode_generation_enabled_for_account(account)
    barcode_can_generate = bool(
        draft is not None
        and draft.provider_product_id is not None
        and not provider_barcodes
        and not common_barcode
        and barcode_generation_enabled
        and draft.barcode_generation_status
        != OzonOfferDraft.BarcodeGenerationStatus.REQUESTING
    )
    return {
        'account': {'id': account.pk, 'name': account.name, 'marketplace': 'ozon'},
        'draft': None if draft is None else {
            'id': draft.pk,
            'offer_id': draft.offer_id,
            'category': None if draft.description_category_id is None else {
                'description_category_id': draft.description_category_id,
                'type_id': draft.type_id,
                'category_path': draft.category_path,
                'type_name': draft.type_name,
                'tree_revision': draft.tree_revision,
            },
            'attribute_schema_revision': draft.attribute_schema_revision,
            'margin_pct': (
                str(draft.margin_pct) if draft.margin_pct is not None else None
            ),
            'price_override': (
                str(draft.price_override) if draft.price_override is not None else None
            ),
            'updated_at': draft.updated_at,
        },
        'attributes': _attribute_presentation(schema, draft),
        'schema': None if schema is None else {
            'revision': schema.schema_hash,
            'attribute_count': schema.attribute_count,
            'required_attribute_count': schema.required_attribute_count,
            'updated_at': schema.updated_at,
        },
        'pricing': pricing,
        'autofill': _autofill_presentation(draft),
        'physical_profile': physical_profile,
        'preflight': _preflight(
            product,
            account,
            draft,
            schema,
            pricing,
            physical_profile,
        ),
        'publication': {
            'write_enabled': ozon_product_write_enabled_for_account(account),
            'status': draft.publication_status if draft is not None else 'not_prepared',
            'provider_product_id': draft.provider_product_id if draft is not None else None,
            'provider_sku': draft.provider_sku if draft is not None else None,
            'provider_status': draft.provider_status if draft is not None else '',
            'moderation_status': draft.moderation_status if draft is not None else '',
            'provider_errors': draft.provider_errors if draft is not None else [],
            'last_provider_sync_at': (
                draft.last_provider_sync_at if draft is not None else None
            ),
            'barcode': {
                'common_value': common_barcode or None,
                'provider_values': provider_barcodes,
                'generation_status': (
                    draft.barcode_generation_status
                    if draft is not None else 'not_requested'
                ),
                'generation_error': (
                    draft.barcode_generation_error if draft is not None else ''
                ),
                'generation_enabled': barcode_generation_enabled,
                'can_generate': barcode_can_generate,
            },
            'latest_operation': operation_presentation(latest_operation),
        },
        'commerce': commerce_presentation(product, account, draft, pricing),
    }


def _dictionary_values(
    account: MarketplaceAccount,
    schema: OzonCategoryAttributeSnapshot,
    definition: dict[str, Any],
    raw_values: list[Any],
) -> list[dict[str, Any]]:
    requested_ids: list[int] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, Mapping):
            raise OzonOfferError(
                'invalid_dictionary_value',
                'Выберите значение из справочника Ozon.',
            )
        raw_id = raw_value.get('dictionary_value_id')
        if isinstance(raw_id, bool) or raw_id is None:
            value_id = 0
        else:
            try:
                value_id = int(raw_id)
            except (TypeError, ValueError):
                value_id = 0
        if value_id <= 0 or value_id in requested_ids:
            raise OzonOfferError(
                'invalid_dictionary_value',
                'Выберите значение из справочника Ozon.',
            )
        requested_ids.append(value_id)

    snapshots = OzonAttributeValueSnapshot.objects.filter(
        account=account,
        description_category_id=schema.description_category_id,
        type_id=schema.type_id,
        attribute_id=definition['id'],
        language=schema.language,
        attribute_schema_hash=schema.schema_hash,
    ).order_by('-updated_at', '-pk')[:MAX_DICTIONARY_SNAPSHOTS_PER_ATTRIBUTE]
    canonical: dict[int, str] = {}
    requested = set(requested_ids)
    for snapshot in snapshots:
        if not isinstance(snapshot.values, list):
            continue
        for candidate in snapshot.values:
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = candidate.get('id')
            candidate_value = candidate.get('value')
            if (
                isinstance(candidate_id, int)
                and not isinstance(candidate_id, bool)
                and candidate_id in requested
                and isinstance(candidate_value, str)
                and candidate_value.strip()
            ):
                canonical.setdefault(candidate_id, candidate_value.strip()[:1000])

    if set(canonical) != requested:
        raise OzonOfferError(
            'invalid_dictionary_value',
            'Значение не найдено в актуальном справочнике Ozon. Повторите поиск.',
        )
    return [
        {
            'value': canonical[value_id],
            'dictionary_value_id': value_id,
        }
        for value_id in requested_ids
    ]


def _normalize_attributes(
    raw: Any,
    account: MarketplaceAccount,
    schema: OzonCategoryAttributeSnapshot,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 200:
        raise OzonOfferError(
            'invalid_attributes',
            'Проверьте характеристики Ozon.',
        )
    definitions = {
        (item['attribute_complex_id'], item['id']): item
        for item in schema.attributes
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise OzonOfferError(
                'invalid_attributes',
                'Проверьте характеристики Ozon.',
            )
        try:
            identity = (int(item.get('complex_id', 0)), int(item['id']))
        except (KeyError, TypeError, ValueError) as exc:
            raise OzonOfferError(
                'invalid_attributes',
                'Проверьте характеристики Ozon.',
            ) from exc
        definition = definitions.get(identity)
        if definition is None or identity in seen:
            raise OzonOfferError(
                'invalid_attribute',
                'Характеристика отсутствует в текущей схеме Ozon.',
            )
        seen.add(identity)
        raw_values = item.get('values')
        if not isinstance(raw_values, list):
            raise OzonOfferError(
                'invalid_attribute_values',
                'Проверьте значение характеристики Ozon.',
            )
        if _is_boolean_attribute(definition) and len(raw_values) > 1:
            raise OzonOfferError(
                'invalid_boolean_value',
                f'{definition["name"]}: выберите только один вариант — «Да» или «Нет».',
            )
        maximum = min(definition['max_value_count'] or 10, 10)
        if len(raw_values) > maximum:
            raise OzonOfferError(
                'too_many_attribute_values',
                'У характеристики слишком много значений.',
            )
        if definition['dictionary_id'] > 0:
            values = _dictionary_values(
                account,
                schema,
                definition,
                raw_values,
            )
        else:
            values = []
            for raw_value in raw_values:
                if not isinstance(raw_value, Mapping):
                    raise OzonOfferError(
                        'invalid_attribute_values',
                        'Проверьте значение характеристики Ozon.',
                    )
                raw_value_text = raw_value.get('value')
                value = (
                    raw_value_text.strip()[:1000]
                    if isinstance(raw_value_text, str)
                    else ''
                )
                dictionary_value_id = raw_value.get('dictionary_value_id', 0)
                if dictionary_value_id not in (0, None, '') or not value:
                    raise OzonOfferError(
                        'invalid_attribute_value',
                        'Введите значение характеристики Ozon.',
                    )
                if _is_boolean_attribute(definition) and value not in {'true', 'false'}:
                    raise OzonOfferError(
                        'invalid_boolean_value',
                        f'{definition["name"]}: выберите только «Да» или «Нет».',
                    )
                values.append({'value': value, 'dictionary_value_id': 0})
        if values:
            normalized.append({
                'id': identity[1],
                'complex_id': identity[0],
                'values': values,
            })
    normalized.sort(key=lambda item: (item['complex_id'], item['id']))
    return normalized


@transaction.atomic
def update_offer_draft(
    product: Product,
    account: MarketplaceAccount,
    *,
    category: tuple[int, int] | None = None,
    attributes: Any = None,
    attributes_supplied: bool = False,
    margin_pct: Decimal | None = None,
    margin_supplied: bool = False,
    price_override: Decimal | None = None,
    price_supplied: bool = False,
) -> OzonOfferDraft:
    if (
        product.tenant_id != account.tenant_id
        or account.marketplace != MarketplaceAccount.MARKETPLACE_OZON
    ):
        raise OzonOfferError(
            'account_scope_mismatch',
            'Товар и аккаунт Ozon должны принадлежать одному tenant-у.',
        )
    Product.objects.select_for_update().get(pk=product.pk, tenant=product.tenant)
    draft = OzonOfferDraft.objects.select_for_update().filter(
        tenant=product.tenant,
        product=product,
        account=account,
    ).first()
    if draft is None:
        draft = OzonOfferDraft(tenant=product.tenant, product=product, account=account)

    if category is not None:
        tree, types = OzonCatalogService.category_types(account)
        selected = next((item for item in types if (
            item['description_category_id'] == category[0] and item['type_id'] == category[1]
        )), None)
        if tree is None or selected is None:
            raise OzonOfferError(
                'invalid_category_type',
                'Выберите конечную категорию из последнего снимка Ozon.',
            )
        changed = (
            draft.description_category_id != category[0]
            or draft.type_id != category[1]
        )
        draft.description_category_id = category[0]
        draft.type_id = category[1]
        draft.category_path = selected['category_path']
        draft.type_name = selected['type_name']
        draft.tree_revision = tree.schema_hash
        if changed:
            draft.attributes = []
            draft.attribute_schema_revision = ''
            draft.autofill = {}

    if attributes_supplied:
        schema = _latest_schema(
            account,
            draft.description_category_id,
            draft.type_id,
        )
        if schema is None:
            raise OzonOfferError(
                'attribute_schema_required',
                'Сначала загрузите характеристики выбранной категории Ozon.',
            )
        draft.attributes = _normalize_attributes(attributes, account, schema)
        draft.attribute_schema_revision = schema.schema_hash
        raw_autofill: Mapping[str, Any] = (
            draft.autofill if isinstance(draft.autofill, Mapping) else {}
        )
        raw_fields = raw_autofill.get('fields')
        fields: dict[str, Any] = {
            str(key): dict(value.items())
            for key, value in raw_fields.items()
            if isinstance(value, Mapping)
        } if isinstance(raw_fields, Mapping) else {}
        selected_identities = {
            f"{item['complex_id']}:{item['id']}" for item in draft.attributes
        }
        for identity in selected_identities:
            previous_field = fields.get(identity)
            fields[identity] = {
                **(
                    dict(previous_field.items())
                    if isinstance(previous_field, Mapping)
                    else {}
                ),
                'state': 'tenant_confirmed',
                'source': 'tenant',
                'source_label': 'Введено вручную',
                'confidence': 0.0,
                'message': (
                    'MAP сохранил значение, но продолжает проверять его формат '
                    'и соответствие данным товара.'
                ),
            }
        raw_recommendations = raw_autofill.get('recommendations')
        recommendations = [
            dict(item.items()) for item in (
                raw_recommendations if isinstance(raw_recommendations, list) else []
            )
            if isinstance(item, Mapping)
            and (
                f"{int(item.get('complex_id') or 0)}:"
                f"{int(item.get('attribute_id') or 0)}"
            ) not in selected_identities
        ]
        draft.autofill = {
            **dict(raw_autofill),
            'status': 'needs_review' if recommendations else 'moderated',
            'updated_at': timezone.now().isoformat(),
            'moderated_at': timezone.now().isoformat(),
            'fields': fields,
            'recommendations': recommendations,
        }
    if margin_supplied:
        draft.margin_pct = margin_pct
        if margin_pct is not None or not price_supplied:
            draft.price_override = None
    if price_supplied:
        draft.price_override = price_override
        if price_override is not None or not margin_supplied:
            draft.margin_pct = None
    draft.save()
    return draft
