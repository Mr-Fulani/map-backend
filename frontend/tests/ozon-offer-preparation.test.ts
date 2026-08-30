import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ozonAttributesPayload,
  replaceOzonAttributeValue,
  type OzonOfferAttribute,
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
