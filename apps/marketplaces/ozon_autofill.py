import re
from collections.abc import Mapping
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
from apps.marketplaces.ozon_catalog import OzonCatalogError, OzonCatalogService
from apps.products.models import Product
from apps.products.physical_profiles import physical_profile_presentation


AUTOFILL_VERSION = 1


class OzonAutofillError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _identity(complex_id: int, attribute_id: int) -> str:
    return f'{complex_id}:{attribute_id}'


def _normalized(value: str) -> str:
    return re.sub(r'[^0-9a-zа-я]+', ' ', value.casefold().replace('ё', 'е')).strip()


def _compact_identity(value: str) -> str:
    return re.sub(r'[^0-9a-zа-я]+', '', value.casefold().replace('ё', 'е'))


def _field_kind(name: str) -> str:
    normalized = _normalized(name)
    if 'тн вэд' in normalized or normalized.startswith('тнвэд'):
        return 'regulatory_tnved'
    if 'маркиров' in normalized or normalized == 'нужен код киз':
        return 'regulatory_marking'
    if normalized == 'бренд' or normalized == 'brand':
        return 'brand'
    if (
        'партномер' in normalized
        or 'артикул производителя' in normalized
        or normalized == 'номер детали'
    ):
        return 'part_number'
    if normalized.startswith('название модели') or normalized == 'модель товара':
        return 'model_name'
    if normalized == 'тип' or normalized == 'тип товара':
        return 'type'
    if 'штрихкод' in normalized or normalized in {'ean', 'ean 13'}:
        return 'barcode'
    return 'unknown'


def _candidate(product: Product, draft: OzonOfferDraft, kind: str) -> dict[str, Any] | None:
    if kind == 'brand' and (product.brand or '').strip():
        return {
            'value': product.brand.strip(),
            'source': 'product.brand',
            'source_label': 'Бренд товара',
            'confidence': 1.0,
        }
    if kind == 'part_number' and product.article.strip():
        return {
            'value': product.article.strip(),
            'source': 'product.article',
            'source_label': 'Артикул товара',
            'confidence': 1.0,
        }
    if kind == 'model_name' and product.article.strip():
        stable_name = ' '.join(filter(None, [
            (product.brand or '').strip(),
            product.article.strip(),
        ]))
        return {
            'value': stable_name,
            'source': 'product.identity',
            'source_label': 'Бренд и артикул товара',
            'confidence': 1.0,
            'message': (
                'MAP использует стабильное значение по бренду и артикулу, '
                'чтобы случайно не объединить разные товары.'
            ),
        }
    if kind == 'type' and draft.type_name.strip():
        return {
            'value': draft.type_name.strip(),
            'source': 'ozon.category_type',
            'source_label': 'Выбранный тип Ozon',
            'confidence': 1.0,
        }
    if kind == 'barcode':
        profile = physical_profile_presentation(product)
        barcode = str(
            profile.get('facts', {}).get('barcode', {}).get('effective_value') or '',
        ).strip()
        if barcode:
            return {
                'value': barcode,
                'source': 'product.physical.barcode',
                'source_label': 'Подтверждённый штрихкод товара',
                'confidence': 1.0,
            }
    return None


def _selected_by_identity(draft: OzonOfferDraft) -> dict[tuple[int, int], list[dict[str, Any]]]:
    selected: dict[tuple[int, int], list[dict[str, Any]]] = {}
    if not isinstance(draft.attributes, list):
        return selected
    for item in draft.attributes:
        if not isinstance(item, Mapping):
            continue
        try:
            identity = (int(item.get('complex_id', 0)), int(item['id']))
        except (KeyError, TypeError, ValueError):
            continue
        values = item.get('values')
        if isinstance(values, list) and values:
            selected[identity] = [
                dict(value.items()) for value in values if isinstance(value, Mapping)
            ]
    return selected


def _latest_schema(
    account: MarketplaceAccount,
    draft: OzonOfferDraft,
) -> OzonCategoryAttributeSnapshot | None:
    if draft.description_category_id is None or draft.type_id is None:
        return None
    return OzonCategoryAttributeSnapshot.objects.filter(
        account=account,
        description_category_id=draft.description_category_id,
        type_id=draft.type_id,
        language='DEFAULT',
    ).order_by('-updated_at', '-pk').first()


def _metadata(
    status: str,
    *,
    applied_count: int = 0,
    preserved_count: int = 0,
    fields: dict[str, dict[str, Any]] | None = None,
    recommendations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        'version': AUTOFILL_VERSION,
        'status': status,
        'updated_at': timezone.now().isoformat(),
        'applied_count': applied_count,
        'preserved_count': preserved_count,
        'fields': fields or {},
        'recommendations': recommendations or [],
    }


def _recommendation(
    code: str,
    attribute: Mapping[str, Any] | None,
    message: str,
    *,
    candidate: str = '',
) -> dict[str, Any]:
    return {
        'code': code,
        'attribute_id': int(attribute['id']) if attribute else None,
        'complex_id': int(attribute.get('attribute_complex_id', 0)) if attribute else None,
        'label': str(attribute.get('name') or 'Характеристики Ozon') if attribute else 'Категория Ozon',
        'message': message,
        'candidate': candidate[:1000],
    }


def _save_waiting_status(
    draft: OzonOfferDraft,
    status: str,
    recommendation: dict[str, Any],
) -> OzonOfferDraft:
    with transaction.atomic():
        locked = OzonOfferDraft.objects.select_for_update().get(pk=draft.pk)
        locked.autofill = _metadata(status, recommendations=[recommendation])
        locked.save(update_fields=['autofill', 'updated_at'])
        return locked


def _current_dictionary_value(
    account: MarketplaceAccount,
    schema: OzonCategoryAttributeSnapshot,
    attribute_id: int,
    raw_value: Mapping[str, Any],
) -> dict[str, Any] | None:
    try:
        value_id = int(raw_value.get('dictionary_value_id') or 0)
    except (TypeError, ValueError):
        return None
    if value_id <= 0:
        return None
    snapshots = OzonAttributeValueSnapshot.objects.filter(
        account=account,
        description_category_id=schema.description_category_id,
        type_id=schema.type_id,
        attribute_id=attribute_id,
        language=schema.language,
        attribute_schema_hash=schema.schema_hash,
    ).order_by('-updated_at', '-pk')[:20]
    for snapshot in snapshots:
        for value in snapshot.values if isinstance(snapshot.values, list) else []:
            if (
                isinstance(value, Mapping)
                and value.get('id') == value_id
                and isinstance(value.get('value'), str)
                and value['value'].strip()
            ):
                return {
                    'value': value['value'].strip()[:1000],
                    'dictionary_value_id': value_id,
                }
    return None


def _exact_dictionary_value(values: Any, candidate: str) -> dict[str, Any] | None:
    expected = _compact_identity(candidate)
    matches = [
        value for value in values if (
            isinstance(value, Mapping)
            and isinstance(value.get('id'), int)
            and isinstance(value.get('value'), str)
            and _compact_identity(value['value']) == expected
        )
    ] if isinstance(values, list) else []
    if len(matches) != 1:
        return None
    return {
        'value': matches[0]['value'].strip()[:1000],
        'dictionary_value_id': matches[0]['id'],
    }


def _dictionary_candidate(
    account: MarketplaceAccount,
    schema: OzonCategoryAttributeSnapshot,
    attribute: Mapping[str, Any],
    candidate: str,
    *,
    allow_provider_reads: bool,
) -> dict[str, Any] | None:
    snapshots = OzonAttributeValueSnapshot.objects.filter(
        account=account,
        description_category_id=schema.description_category_id,
        type_id=schema.type_id,
        attribute_id=attribute['id'],
        language=schema.language,
        query__iexact=candidate,
        attribute_schema_hash=schema.schema_hash,
    ).order_by('-updated_at', '-pk')
    snapshot = snapshots.first()
    if snapshot is None and allow_provider_reads:
        snapshot = OzonCatalogService.search_attribute_values(
            account,
            description_category_id=schema.description_category_id,
            type_id=schema.type_id,
            attribute_id=attribute['id'],
            query=candidate,
            language=schema.language,
            confirmed=True,
        )
    return _exact_dictionary_value(snapshot.values, candidate) if snapshot else None


def _schema_for_autofill(
    account: MarketplaceAccount,
    draft: OzonOfferDraft,
    *,
    allow_provider_reads: bool,
) -> OzonCategoryAttributeSnapshot | None:
    schema = _latest_schema(account, draft)
    if schema is not None or not allow_provider_reads:
        return schema
    category_id = draft.description_category_id
    type_id = draft.type_id
    if category_id is None or type_id is None:
        return None
    return OzonCatalogService.sync_attributes(
        account,
        description_category_id=category_id,
        type_id=type_id,
        language='DEFAULT',
        confirmed=True,
    )


def autofill_ozon_offer(
    product: Product,
    account: MarketplaceAccount,
    *,
    allow_provider_reads: bool,
) -> OzonOfferDraft:
    if (
        product.tenant_id != account.tenant_id
        or account.marketplace != MarketplaceAccount.MARKETPLACE_OZON
    ):
        raise OzonAutofillError(
            'account_scope_mismatch',
            'Товар и кабинет Ozon должны принадлежать одному тенанту.',
        )

    draft, _created = OzonOfferDraft.objects.get_or_create(
        tenant=product.tenant,
        product=product,
        account=account,
    )
    if draft.description_category_id is None or draft.type_id is None:
        return _save_waiting_status(
            draft,
            'category_required',
            _recommendation(
                'category_confirmation_required',
                None,
                'Выберите конечный тип Ozon. MAP не назначает категорию '
                'автоматически, если нет однозначного подтверждения.',
            ),
        )

    tree, types = OzonCatalogService.category_types(account)
    current_type = next((item for item in types if (
        item['description_category_id'] == draft.description_category_id
        and item['type_id'] == draft.type_id
    )), None)
    if tree is None or current_type is None or draft.tree_revision != tree.schema_hash:
        return _save_waiting_status(
            draft,
            'category_review_required',
            _recommendation(
                'category_review_required',
                None,
                'Дерево Ozon изменилось. Подтвердите категорию товара ещё раз.',
            ),
        )

    try:
        schema = _schema_for_autofill(
            account,
            draft,
            allow_provider_reads=allow_provider_reads,
        )
    except OzonCatalogError as exc:
        safe_message = {
            'rate_limited': 'Ozon временно ограничил чтение справочника. Повторите позже.',
            'provider_disabled': 'Автозаполнение закрыто настройками подключения этого кабинета.',
            'invalid_credentials': 'Проверьте подключение кабинета Ozon.',
        }.get(exc.code, 'Не удалось загрузить актуальную схему характеристик Ozon.')
        return _save_waiting_status(
            draft,
            'schema_unavailable',
            _recommendation('schema_unavailable', None, safe_message),
        )
    if schema is None:
        return _save_waiting_status(
            draft,
            'schema_required',
            _recommendation(
                'schema_required',
                None,
                'Загрузите схему характеристик Ozon или повторите безопасное автозаполнение.',
            ),
        )

    product.refresh_from_db()
    draft.refresh_from_db()
    selected = _selected_by_identity(draft)
    previous_fields = (
        draft.autofill.get('fields', {})
        if isinstance(draft.autofill, Mapping)
        and isinstance(draft.autofill.get('fields'), Mapping)
        else {}
    )
    current_schema = draft.attribute_schema_revision == schema.schema_hash
    prepared: list[dict[str, Any]] = []
    fields: dict[str, dict[str, Any]] = {}
    recommendations: list[dict[str, Any]] = []
    applied_count = 0
    preserved_count = 0

    for attribute in schema.attributes:
        identity = (
            int(attribute.get('attribute_complex_id', 0)),
            int(attribute['id']),
        )
        key = _identity(*identity)
        existing_values = selected.get(identity, [])
        preserved_values: list[dict[str, Any]] = []
        if existing_values:
            if current_schema and attribute['dictionary_id'] == 0:
                preserved_values = [dict(existing_values[0])]
            elif current_schema and attribute['dictionary_id'] > 0:
                current_value = _current_dictionary_value(
                    account,
                    schema,
                    attribute['id'],
                    existing_values[0],
                )
                if current_value:
                    preserved_values = [current_value]
            elif attribute['dictionary_id'] == 0:
                raw_text = str(existing_values[0].get('value') or '').strip()[:1000]
                if raw_text:
                    preserved_values = [{
                        'value': raw_text,
                        'dictionary_value_id': 0,
                    }]

        previous = previous_fields.get(key) if isinstance(previous_fields, Mapping) else None
        previous_was_auto = isinstance(previous, Mapping) and previous.get('state') == 'auto_filled'
        kind = _field_kind(str(attribute.get('name') or ''))
        candidate = _candidate(product, draft, kind)

        if preserved_values and not previous_was_auto:
            prepared.append({
                'id': identity[1],
                'complex_id': identity[0],
                'values': preserved_values,
            })
            preserved_count += 1
            fields[key] = {
                'state': 'kept_manual',
                'source': 'tenant',
                'source_label': 'Введено вручную',
                'confidence': 0.0,
                'message': (
                    'MAP сохранил введённое значение и не перезаписал его. '
                    'Ручной ввод не считается автоматическим подтверждением достоверности.'
                ),
            }
            continue

        if candidate is not None:
            candidate_value: dict[str, Any] | None
            if attribute['dictionary_id'] > 0:
                try:
                    candidate_value = _dictionary_candidate(
                        account,
                        schema,
                        attribute,
                        candidate['value'],
                        allow_provider_reads=allow_provider_reads,
                    )
                except OzonCatalogError:
                    candidate_value = None
            else:
                candidate_value = {
                    'value': candidate['value'][:1000],
                    'dictionary_value_id': 0,
                }
            if candidate_value is not None:
                prepared.append({
                    'id': identity[1],
                    'complex_id': identity[0],
                    'values': [candidate_value],
                })
                applied_count += 1
                fields[key] = {
                    'state': 'auto_filled',
                    'source': candidate['source'],
                    'source_label': candidate['source_label'],
                    'confidence': candidate['confidence'],
                    'message': candidate.get(
                        'message',
                        'Значение подтверждено данными товара и справочником Ozon.',
                    ),
                }
                continue
            recommendations.append(_recommendation(
                'dictionary_match_needs_review',
                attribute,
                'MAP не нашёл единственного точного совпадения в справочнике Ozon. Выберите значение вручную.',
                candidate=candidate['value'],
            ))
            continue

        if preserved_values:
            prepared.append({
                'id': identity[1],
                'complex_id': identity[0],
                'values': preserved_values,
            })
            preserved_count += 1
            fields[key] = {
                'state': 'kept_previous',
                'source': 'ozon_draft',
                'source_label': 'Сохранённое значение Ozon',
                'confidence': 1.0,
                'message': 'Значение сохранилось после повторной проверки схемы.',
            }
            continue

        if not attribute.get('is_required'):
            continue
        if kind == 'regulatory_tnved':
            recommendations.append(_recommendation(
                'tnved_confirmation_required',
                attribute,
                'Код ТН ВЭД нельзя придумывать. Укажите подтверждённый код '
                'от 1С, бухгалтера или таможенного специалиста.',
            ))
        elif kind == 'regulatory_marking':
            recommendations.append(_recommendation(
                'marking_confirmation_required',
                attribute,
                'Признак маркировки нельзя определять по названию товара. '
                'Подтвердите его по документам или требованиям товарной группы.',
            ))
        else:
            recommendations.append(_recommendation(
                'required_attribute_needs_review',
                attribute,
                'У MAP нет надёжного источника для этого обязательного поля. Заполните его вручную.',
            ))

    from apps.marketplaces.ozon_offers import _normalize_attributes

    with transaction.atomic():
        locked = OzonOfferDraft.objects.select_for_update().get(
            pk=draft.pk,
            tenant=product.tenant,
            product=product,
            account=account,
        )
        if (
            locked.description_category_id != schema.description_category_id
            or locked.type_id != schema.type_id
        ):
            raise OzonAutofillError(
                'draft_changed',
                'Категория изменилась во время подготовки. Повторите автозаполнение.',
            )
        locked.attributes = _normalize_attributes(prepared, account, schema)
        locked.attribute_schema_revision = schema.schema_hash
        locked.autofill = _metadata(
            'needs_review' if recommendations else 'completed',
            applied_count=applied_count,
            preserved_count=preserved_count,
            fields=fields,
            recommendations=recommendations,
        )
        locked.save(update_fields=[
            'attributes',
            'attribute_schema_revision',
            'autofill',
            'updated_at',
        ])
        return locked


def autofill_active_ozon_offers(product_id: int) -> dict[str, Any]:
    product = Product.objects.select_related('tenant', 'physical_profile').filter(
        pk=product_id,
    ).first()
    if product is None:
        return {'status': 'not_found', 'processed': 0, 'failed': 0}
    accounts = MarketplaceAccount.objects.filter(
        tenant=product.tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        is_active=True,
        deleted_at__isnull=True,
        ozon_profile__connection_status=OzonAccountProfile.ConnectionStatus.CONNECTED,
    ).order_by('pk')
    processed = 0
    failed = 0
    for account in accounts:
        try:
            autofill_ozon_offer(product, account, allow_provider_reads=True)
            processed += 1
        except (OzonAutofillError, OzonCatalogError):
            failed += 1
    return {'status': 'ok', 'processed': processed, 'failed': failed}


def schedule_ozon_autofill(product_id: int, *, trigger_key: str) -> bool:
    product = Product.objects.only('tenant_id').filter(pk=product_id).first()
    if product is None or not MarketplaceAccount.objects.filter(
        tenant_id=product.tenant_id,
        marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        is_active=True,
        deleted_at__isnull=True,
        ozon_profile__connection_status=OzonAccountProfile.ConnectionStatus.CONNECTED,
    ).exists():
        return False
    from apps.core.dispatch import enqueue_durable_task

    enqueue_durable_task(
        'apps.marketplaces.tasks.prepare_ozon_offers_after_enrichment',
        args=[product_id],
        deduplication_key=f'ozon-autofill:{trigger_key}'[:240],
        max_run_attempts=4,
    )
    return True
