import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ozonMarginFromPrice,
  ozonPriceFromMargin,
  ozonPricingPayload,
} from '../src/lib/ozon-offer-pricing';

test('inherited Ozon price clears both offer overrides', () => {
  assert.deepEqual(ozonPricingPayload('inherited', '25', '1250'), {
    ok: true,
    payload: { margin_pct: null, price_override: null },
  });
});

test('Ozon pricing payload keeps margin and exact price mutually exclusive', () => {
  assert.deepEqual(ozonPricingPayload('margin', '12,50', '1125'), {
    ok: true,
    payload: { margin_pct: '12.50', price_override: null },
  });
  assert.deepEqual(ozonPricingPayload('price', '59', '1 035,11'), {
    ok: false,
    message: 'Цена Ozon должна быть числом больше нуля с точностью до копеек.',
  });
  assert.deepEqual(ozonPricingPayload('price', '59', '1035,11'), {
    ok: true,
    payload: { margin_pct: null, price_override: '1035.11' },
  });
});

test('Ozon pricing validation rejects unsafe numeric values', () => {
  assert.equal(ozonPricingPayload('margin', '-100', '').ok, false);
  assert.equal(ozonPricingPayload('margin', '12.345', '').ok, false);
  assert.equal(ozonPricingPayload('price', '', '0').ok, false);
  assert.equal(ozonPricingPayload('price', '', '100.001').ok, false);
});

test('Ozon margin and price calculations use the same reversible formula', () => {
  assert.equal(ozonPriceFromMargin('650.84', '59.04'), '1035.10');
  assert.equal(ozonMarginFromPrice('650.84', '1035.11'), '59.04');
  assert.equal(ozonPriceFromMargin('0', '25'), null);
  assert.equal(ozonMarginFromPrice('650.84', '0'), null);
});
