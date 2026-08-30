from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.marketplaces.models import (
    MarketplaceAccount,
    OzonAccountProfile,
    OzonAttributeValueSnapshot,
    OzonCategoryAttributeSnapshot,
    OzonOfferDraft,
)
from apps.marketplaces.ozon_catalog import OzonCatalogService
from apps.products.models import Product, ProductImage
from apps.products.physical_profiles import physical_profile_presentation


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

MAX_DICTIONARY_SNAPSHOTS_PER_ATTRIBUTE = 20


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


def _preflight(
    product: Product,
    account: MarketplaceAccount,
    draft: OzonOfferDraft | None,
    schema: OzonCategoryAttributeSnapshot | None,
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
            for attribute in schema.attributes:
                identity = (
                    attribute['attribute_complex_id'],
                    attribute['id'],
                )
                if attribute['is_required'] and not selected.get(identity):
                    errors.append(_issue(
                        'required_attribute_missing',
                        f'attribute:{identity[0]}:{identity[1]}',
                        attribute['name'],
                        'Заполните обязательную характеристику Ozon.',
                    ))

    physical = physical_profile_presentation(product)
    for field in physical['missing_fields']:
        errors.append(_issue(
            'physical_fact_missing', f'physical:{field}', PHYSICAL_LABELS[field],
            'Заполните значение из 1С или MAP в блоке «Данные для Ozon».',
        ))
    if not (product.title_ai or product.name).strip():
        errors.append(_issue('name_missing', 'name', 'Название', 'У товара нет названия.'))
    if not (product.brand or '').strip():
        errors.append(_issue('brand_missing', 'brand', 'Бренд', 'Укажите бренд товара.'))
    if Decimal(product.price) <= 0:
        errors.append(_issue('price_missing', 'price', 'Цена', 'Цена должна быть больше нуля.'))
    if not product.images.exclude(status=ProductImage.Status.REJECTED).exists():
        errors.append(_issue('image_missing', 'images', 'Фотографии', 'Добавьте хотя бы одну фотографию.'))
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
            'updated_at': draft.updated_at,
        },
        'attributes': _attribute_presentation(schema, draft),
        'schema': None if schema is None else {
            'revision': schema.schema_hash,
            'attribute_count': schema.attribute_count,
            'required_attribute_count': schema.required_attribute_count,
            'updated_at': schema.updated_at,
        },
        'preflight': _preflight(product, account, draft, schema),
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
                value = str(raw_value.get('value') or '').strip()[:1000]
                dictionary_value_id = raw_value.get('dictionary_value_id', 0)
                if dictionary_value_id not in (0, None, '') or not value:
                    raise OzonOfferError(
                        'invalid_attribute_value',
                        'Введите значение характеристики Ozon.',
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
    draft.save()
    return draft
