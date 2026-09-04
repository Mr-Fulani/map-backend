import type { OzonCategoryPolicyState } from './marketplace-account-types';

export interface OzonOfferValue {
  value: string;
  dictionary_value_id: number;
}

export interface OzonOfferAttribute {
  id: number;
  complex_id: number;
  attribute_complex_id: number;
  name: string;
  description: string;
  type: string;
  is_required: boolean;
  max_value_count: number;
  dictionary_id: number;
  selected_values: OzonOfferValue[];
}

export interface OzonPreflightIssue {
  code: string;
  field: string;
  label: string;
  message: string;
}

export interface OzonAutofillField {
  state:
    | 'auto_filled'
    | 'kept_manual'
    | 'kept_previous'
    | 'tenant_confirmed'
    | 'tenant_entered';
  source: string;
  source_label: string;
  confidence: number;
  message: string;
}

export interface OzonAutofillRecommendation {
  code: string;
  attribute_id: number | null;
  complex_id: number | null;
  label: string;
  message: string;
  candidate: string;
}

export interface OzonAutofillState {
  status: string;
  updated_at: string | null;
  moderated_at: string | null;
  applied_count: number;
  preserved_count: number;
  fields: Record<string, OzonAutofillField>;
  recommendations: OzonAutofillRecommendation[];
}

export type OzonOperationState =
  | 'queued'
  | 'sending'
  | 'outcome_unknown'
  | 'reconciling'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'manual_review';

export interface OzonOperationPresentation {
  id: string;
  kind: string;
  state: OzonOperationState;
  provider_task_id: string | null;
  errors: Array<{
    code: string;
    message: string;
    provider_code?: string;
    field?: string;
    attribute_id?: number | null;
  }>;
  attempt_count: number;
  reconcile_count: number;
  last_reconciled_at: string | null;
  next_reconcile_at: string | null;
  retry_after_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface OzonOfferPreparation {
  account: { id: number; name: string; marketplace: 'ozon' };
  draft: null | {
    id: number;
    offer_id: string;
    category: null | {
      description_category_id: number;
      type_id: number;
      category_path: string;
      type_name: string;
      tree_revision: string;
    };
    attribute_schema_revision: string;
    margin_pct: string | null;
    price_override: string | null;
    updated_at: string;
  };
  attributes: OzonOfferAttribute[];
  schema: null | {
    revision: string;
    attribute_count: number;
    required_attribute_count: number;
    updated_at: string;
  };
  pricing: null | {
    base_price: string;
    effective_margin_pct: string;
    margin_override: string | null;
    price_override: string | null;
    margin_source: 'offer_margin' | 'offer_price' | 'category';
    final_price: string;
    policy: OzonCategoryPolicyState;
  };
  autofill: OzonAutofillState;
  preflight: {
    ready: boolean;
    errors: OzonPreflightIssue[];
    recommendations: OzonPreflightIssue[];
  };
  publication: {
    write_enabled: boolean;
    status: string;
    provider_product_id: number | null;
    provider_sku: number | null;
    provider_status: string;
    moderation_status: string;
    provider_errors: Array<{ code?: string; message?: string }>;
    last_provider_sync_at: string | null;
    latest_operation: OzonOperationPresentation | null;
  };
  commerce: {
    can_sync: boolean;
    desired_price: string | null;
    desired_stock: number;
    warehouse_id: string;
    warehouse_name: string;
    last_synced_price: string | null;
    last_price_sync_at: string | null;
    last_synced_stock: number | null;
    last_stock_sync_at: string | null;
    last_stock_warehouse_id: string;
    price_operation: OzonOperationPresentation | null;
    stock_operation: OzonOperationPresentation | null;
  };
}

const ACTIVE_OZON_OPERATION_STATES = new Set<OzonOperationState>([
  'queued',
  'sending',
  'outcome_unknown',
  'reconciling',
]);

export function ozonPublicationDisabled(preparation: OzonOfferPreparation | null): boolean {
  if (!preparation?.preflight.ready || !preparation.publication.write_enabled) return true;
  const state = preparation.publication.latest_operation?.state;
  return state
    ? ACTIVE_OZON_OPERATION_STATES.has(state) || ['partial', 'manual_review'].includes(state)
    : false;
}

export function ozonCanReconcile(preparation: OzonOfferPreparation | null): boolean {
  const state = preparation?.publication.latest_operation?.state;
  return state ? ACTIVE_OZON_OPERATION_STATES.has(state) : false;
}

export function ozonPublicationActionLabel(preparation: OzonOfferPreparation | null): string {
  const state = preparation?.publication.latest_operation?.state;
  if (state === 'failed') return 'Исправил, отправить повторно';
  if (state === 'succeeded') return 'Отправить обновление в Ozon';
  return 'Отправить в Ozon';
}

export function ozonPublicationStatusLabel(preparation: OzonOfferPreparation | null): string {
  const state = preparation?.publication.latest_operation?.state;
  if (!state) return 'Не отправлялась';
  if (state === 'queued' || state === 'sending') return 'Отправляется';
  if (state === 'outcome_unknown') return 'Ответ нужно сверить';
  if (state === 'reconciling') return 'Проверяется в Ozon';
  if (state === 'succeeded') return 'Опубликована';
  if (state === 'failed') return 'Ozon отклонил карточку';
  return 'Нужна ручная проверка';
}

export function ozonPublicationMessage(preparation: OzonOfferPreparation | null): string {
  if (!preparation) return 'Откройте и подготовьте карточку Ozon.';
  if (!preparation.preflight.ready) {
    return `Сначала исправьте обязательные поля: ${preparation.preflight.errors.length}.`;
  }
  if (!preparation.publication.write_enabled) {
    return 'Карточка готова, но отправка для этого кабинета пока закрыта безопасным rollout.';
  }
  const operation = preparation.publication.latest_operation;
  if (!operation) return 'Карточка готова к ручной отправке в Ozon.';
  if (operation.state === 'outcome_unknown') {
    return 'Ozon мог получить карточку. Не отправляйте повторно — MAP сначала сверит результат.';
  }
  if (operation.state === 'reconciling') {
    return `Ozon принял задачу ${operation.provider_task_id ?? ''}. MAP ожидает результат проверки.`;
  }
  if (operation.state === 'sending' || operation.state === 'queued') {
    return 'Отправка уже выполняется. Повторный запрос не нужен.';
  }
  if (operation.state === 'succeeded') return 'Карточка опубликована и сверена с Ozon.';
  if (operation.state === 'partial' || operation.state === 'manual_review') {
    return 'Нужна ручная проверка результата Ozon перед повторной отправкой.';
  }
  if (operation.state === 'failed') {
    return operation.errors[0]?.message ?? 'Ozon отклонил отправку. Исправьте ошибку и повторите.';
  }
  return 'Карточка готова к ручной отправке в Ozon.';
}

export function ozonAttributeIdentity(attribute: Pick<OzonOfferAttribute, 'id' | 'complex_id'>) {
  return `${attribute.complex_id}:${attribute.id}`;
}

export interface OzonDictionaryValue {
  id: number;
  value: string;
  info: string;
  picture: string;
}

export interface OzonAttributeGuidance {
  owner: 'map' | 'tenant' | 'documents';
  ownerLabel: string;
  source: string;
  note: string;
}

function normalizedAttributeName(name: string): string {
  return name
    .toLocaleLowerCase('ru-RU')
    .replaceAll('ё', 'е')
    .replace(/[^0-9a-zа-я]+/g, ' ')
    .trim();
}

export function ozonAttributeGuidance(
  attribute: Pick<OzonOfferAttribute, 'name' | 'dictionary_id'>,
): OzonAttributeGuidance {
  const name = normalizedAttributeName(attribute.name);
  if (name.includes('тн вэд') || name.startsWith('тнвэд')) {
    return {
      owner: 'documents',
      ownerLabel: 'Нужно подтвердить по документам',
      source: 'Декларация или сертификат соответствия, документы поставщика либо таможенный специалист.',
      note: 'MAP может показать кандидат, но не должен сам утверждать код ТН ВЭД.',
    };
  }
  if (name.includes('маркиров') || name.includes('код киз')) {
    return {
      owner: 'documents',
      ownerLabel: 'Нужно решение Тенанта',
      source: 'Документы поставщика и требования маркировки для этой товарной группы.',
      note: 'Выберите только «Да» или «Нет». MAP не угадывает обязательность маркировки по названию.',
    };
  }
  if (name === 'бренд' || name === 'brand') {
    return {
      owner: 'map',
      ownerLabel: 'MAP заполняет при точном совпадении',
      source: 'Бренд товара и официальный справочник значений Ozon.',
      note: 'Если MAP не нашёл единственное совпадение, выберите бренд из справочника и проверьте написание.',
    };
  }
  if (name.includes('партномер') || name.includes('артикул производителя')) {
    return {
      owner: 'map',
      ownerLabel: 'MAP заполняет из товара',
      source: 'Артикул производителя из 1С, каталога производителя или карточки поставщика.',
      note: 'Не подменяйте партномер OEM-номером другой детали.',
    };
  }
  if (name.startsWith('название модели') || name === 'модель товара') {
    return {
      owner: 'map',
      ownerLabel: 'MAP создаёт безопасный вариант',
      source: 'Бренд и артикул товара.',
      note: 'Одинаковое значение объединяет варианты, поэтому его нужно проверить перед массовой публикацией.',
    };
  }
  if (name === 'тип' || name === 'тип товара') {
    return {
      owner: 'map',
      ownerLabel: 'MAP предлагает по категории',
      source: 'Выбранный конечный тип из дерева Ozon.',
      note: 'Проверьте, что тип соответствует названию товара, а не только общей ветке каталога.',
    };
  }
  if (name.includes('штрихкод') || name === 'ean' || name === 'ean 13') {
    return {
      owner: 'tenant',
      ownerLabel: 'Только из подтверждённого источника',
      source: 'Упаковка, 1С, карточка поставщика или каталог производителя.',
      note: 'MAP не придумывает штрихкоды и не использует вместо них артикул.',
    };
  }
  return {
    owner: attribute.dictionary_id > 0 ? 'tenant' : 'map',
    ownerLabel: attribute.dictionary_id > 0
      ? 'Выберите из справочника Ozon'
      : 'MAP заполняет, если есть точный факт',
    source: attribute.dictionary_id > 0
      ? 'Официальный справочник выбранной категории Ozon.'
      : '1С, подтверждённые данные товара или каталог производителя.',
    note: 'Если точного источника нет, значение остаётся на проверку Тенанту.',
  };
}

const OZON_BOOLEAN_VALUES = new Set(['true', 'false']);
const OZON_BOOLEAN_ATTRIBUTE_NAMES = new Set([
  'нужен код маркировки',
]);

export function isOzonBooleanAttribute(
  attribute: Pick<OzonOfferAttribute, 'dictionary_id' | 'name' | 'type'>,
) {
  if (attribute.dictionary_id > 0) return false;
  const type = attribute.type.trim().toLocaleLowerCase('ru-RU');
  const name = attribute.name.trim().toLocaleLowerCase('ru-RU');
  return type === 'boolean' || type === 'bool' || OZON_BOOLEAN_ATTRIBUTE_NAMES.has(name);
}

export function ozonAttributeValidationMessage(attribute: OzonOfferAttribute): string | null {
  if (attribute.selected_values.length === 0) return null;
  if (attribute.dictionary_id > 0) {
    return attribute.selected_values.every((value) => (
      Number.isSafeInteger(value.dictionary_value_id)
      && value.dictionary_value_id > 0
      && value.value.trim().length > 0
    ))
      ? null
      : 'Выберите значение из справочника Ozon.';
  }
  if (isOzonBooleanAttribute(attribute)) {
    return attribute.selected_values.length === 1
      && OZON_BOOLEAN_VALUES.has(attribute.selected_values[0].value)
      && attribute.selected_values[0].dictionary_value_id === 0
      ? null
      : 'Выберите только «Да» или «Нет».';
  }
  return attribute.selected_values.every((value) => (
    value.dictionary_value_id === 0 && value.value.trim().length > 0
  ))
    ? null
    : 'Проверьте значение характеристики Ozon.';
}

export function ozonAttributesValidationErrors(attributes: OzonOfferAttribute[]) {
  return attributes.flatMap((attribute) => {
    const message = ozonAttributeValidationMessage(attribute);
    return message ? [{ attribute, message }] : [];
  });
}

export function ozonAttributesPayload(attributes: OzonOfferAttribute[]) {
  return attributes
    .filter((attribute) => attribute.selected_values.length > 0)
    .map((attribute) => ({
      id: attribute.id,
      complex_id: attribute.complex_id,
      values: attribute.selected_values.map((value) => ({
        value: value.value.trim(),
        dictionary_value_id: value.dictionary_value_id,
      })),
    }));
}

export function replaceOzonAttributeValue(
  attributes: OzonOfferAttribute[],
  attributeId: number,
  complexId: number,
  value: OzonOfferValue | null,
): OzonOfferAttribute[] {
  return attributes.map((attribute) => (
    attribute.id === attributeId && attribute.complex_id === complexId
      ? { ...attribute, selected_values: value ? [value] : [] }
      : attribute
  ));
}
