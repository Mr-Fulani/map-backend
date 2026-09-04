import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isOzonBooleanAttribute,
  ozonAttributeGuidance,
  ozonAttributesValidationErrors,
  ozonAttributesPayload,
  ozonCanReconcile,
  ozonPublicationActionLabel,
  ozonPublicationDisabled,
  ozonPublicationMessage,
  ozonPublicationStatusLabel,
  replaceOzonAttributeValue,
  type OzonOfferAttribute,
  type OzonOfferPreparation,
  type OzonOperationPresentation,
} from '../src/lib/ozon-offer-preparation';


const attributes: OzonOfferAttribute[] = [{
  id: 85,
  complex_id: 0,
  attribute_complex_id: 0,
  name: 'Бренд',
  description: '',
  type: 'String',
  is_required: true,
  max_value_count: 1,
  dictionary_id: 42,
  selected_values: [],
}, {
  id: 100,
  complex_id: 3,
  attribute_complex_id: 3,
  name: 'Материал',
  description: '',
  type: 'String',
  is_required: false,
  max_value_count: 1,
  dictionary_id: 0,
  selected_values: [{ value: ' Сталь ', dictionary_value_id: 0 }],
}];


test('replaces only the exact Ozon attribute identity', () => {
  const updated = replaceOzonAttributeValue(
    attributes,
    85,
    0,
    { value: 'Test Brand', dictionary_value_id: 501 },
  );
  assert.deepEqual(updated[0].selected_values, [{
    value: 'Test Brand',
    dictionary_value_id: 501,
  }]);
  assert.deepEqual(updated[1], attributes[1]);
  assert.deepEqual(attributes[0].selected_values, []);
});

test('sends provider IDs and omits empty attributes', () => {
  const updated = replaceOzonAttributeValue(
    attributes,
    85,
    0,
    { value: 'Test Brand', dictionary_value_id: 501 },
  );
  assert.deepEqual(ozonAttributesPayload(updated), [{
    id: 85,
    complex_id: 0,
    values: [{ value: 'Test Brand', dictionary_value_id: 501 }],
  }, {
    id: 100,
    complex_id: 3,
    values: [{ value: 'Сталь', dictionary_value_id: 0 }],
  }]);
});

test('recognizes boolean Ozon attributes and rejects arbitrary text', () => {
  const marking: OzonOfferAttribute = {
    id: 200,
    complex_id: 0,
    attribute_complex_id: 0,
    name: 'Нужен код маркировки',
    description: 'Выберите Да или Нет',
    type: 'String',
    is_required: true,
    max_value_count: 1,
    dictionary_id: 0,
    selected_values: [{ value: '4009320000', dictionary_value_id: 0 }],
  };

  assert.equal(isOzonBooleanAttribute(marking), true);
  assert.deepEqual(ozonAttributesValidationErrors([marking]).map((item) => item.message), [
    'Выберите только «Да» или «Нет».',
  ]);
  const corrected = replaceOzonAttributeValue(
    [marking],
    marking.id,
    marking.complex_id,
    { value: 'false', dictionary_value_id: 0 },
  );
  assert.deepEqual(ozonAttributesValidationErrors(corrected), []);
  assert.deepEqual(ozonAttributesPayload(corrected), [{
    id: 200,
    complex_id: 0,
    values: [{ value: 'false', dictionary_value_id: 0 }],
  }]);
});

test('explains who owns safe and regulatory Ozon attributes', () => {
  assert.deepEqual(
    ozonAttributeGuidance({ name: 'Бренд', dictionary_id: 42 }),
    {
      owner: 'map',
      ownerLabel: 'MAP заполняет при точном совпадении',
      source: 'Бренд товара и официальный справочник значений Ozon.',
      note: 'Если точного бренда нет, не выбирайте похожий. Для действительно безымянного товара выберите «Нет бренда», иначе подайте заявку на бренд в Ozon.',
    },
  );
  assert.equal(
    ozonAttributeGuidance({ name: 'ТН ВЭД коды ЕАЭС', dictionary_id: 12 }).owner,
    'documents',
  );
  assert.match(
    ozonAttributeGuidance({ name: 'Нужен код маркировки', dictionary_id: 0 }).note,
    /не угадывает/,
  );
});

function publicationPreparation(
  state: OzonOperationPresentation | null,
): OzonOfferPreparation {
  return {
    account: { id: 5, name: 'AlfaPro Ozon', marketplace: 'ozon' },
    draft: null,
    attributes: [],
    schema: null,
    pricing: null,
    autofill: {
      status: 'not_started',
      updated_at: null,
      moderated_at: null,
      applied_count: 0,
      preserved_count: 0,
      fields: {},
      recommendations: [],
    },
    preflight: { ready: true, errors: [], recommendations: [] },
    publication: {
      write_enabled: true,
      status: 'local_draft',
      provider_product_id: null,
      provider_sku: null,
      provider_status: '',
      moderation_status: '',
      provider_errors: [],
      last_provider_sync_at: null,
      latest_operation: state,
    },
    commerce: {
      can_sync: false, desired_price: null, desired_stock: 0,
      warehouse_id: '', warehouse_name: '', last_synced_price: null,
      last_price_sync_at: null, last_synced_stock: null, last_stock_sync_at: null,
      last_stock_warehouse_id: '', price_operation: null, stock_operation: null,
    },
  };
}

test('blocks duplicate Ozon send while an outcome is active or unknown', () => {
  const unknown = publicationPreparation({
    id: 'operation-1',
    kind: 'product_import',
    state: 'outcome_unknown',
    provider_task_id: null,
    errors: [],
    attempt_count: 1,
    reconcile_count: 0,
    last_reconciled_at: null,
    next_reconcile_at: null,
    retry_after_at: null,
    completed_at: null,
    created_at: '2026-09-02T10:00:00Z',
    updated_at: '2026-09-02T10:00:01Z',
  });

  assert.equal(ozonPublicationDisabled(unknown), true);
  assert.equal(ozonCanReconcile(unknown), true);
  assert.equal(ozonPublicationStatusLabel(unknown), 'Ответ нужно сверить');
  assert.match(ozonPublicationMessage(unknown), /Не отправляйте повторно/);
  assert.equal(ozonPublicationDisabled(publicationPreparation(null)), false);
});

test('Ozon retry copy is explicit only after a terminal rejection', () => {
  const failed = publicationPreparation({
    id: 'operation-2',
    kind: 'product_import',
    state: 'failed',
    provider_task_id: 'task-2',
    errors: [{ code: 'import_failed', message: 'Исправьте обязательное поле.' }],
    attempt_count: 1,
    reconcile_count: 1,
    last_reconciled_at: '2026-09-02T10:01:00Z',
    next_reconcile_at: null,
    retry_after_at: null,
    completed_at: '2026-09-02T10:01:00Z',
    created_at: '2026-09-02T10:00:00Z',
    updated_at: '2026-09-02T10:01:00Z',
  });

  assert.equal(ozonCanReconcile(failed), false);
  assert.equal(ozonPublicationDisabled(failed), false);
  assert.equal(ozonPublicationStatusLabel(failed), 'Ozon отклонил карточку');
  assert.equal(ozonPublicationActionLabel(failed), 'Исправил, отправить повторно');
  assert.equal(ozonPublicationMessage(failed), 'Исправьте обязательное поле.');
});
