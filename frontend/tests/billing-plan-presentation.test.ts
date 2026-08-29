import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AVITO_ACCOUNT_ACTIVE_LISTING_LIMIT,
  getListingLimitPresentation,
} from '../src/lib/billing-plan-presentation';


test('a plan below the Avito account ceiling shows the paid tenant limit twice', () => {
  const presentation = getListingLimitPresentation(1_000);

  assert.match(presentation.total, /1\s000 активных объявлений всего в MAP/);
  assert.match(presentation.perAvitoAccount, /1\s000 в одном Avito-аккаунте/);
  assert.equal(presentation.requiresMultipleAvitoAccounts, false);
});


test('the Business allowance fits into one Avito account', () => {
  const presentation = getListingLimitPresentation(AVITO_ACCOUNT_ACTIVE_LISTING_LIMIT);

  assert.match(presentation.total, /10\s000 активных объявлений всего в MAP/);
  assert.match(presentation.perAvitoAccount, /10\s000 в одном Avito-аккаунте/);
  assert.equal(presentation.requiresMultipleAvitoAccounts, false);
});


test('the Pro allowance is tenant-wide but one Avito account stays capped at ten thousand', () => {
  const presentation = getListingLimitPresentation(50_000);

  assert.match(presentation.total, /50\s000 активных объявлений всего в MAP/);
  assert.match(presentation.perAvitoAccount, /10\s000 в одном Avito-аккаунте/);
  assert.equal(presentation.requiresMultipleAvitoAccounts, true);
});


test('an unlimited tenant plan still exposes the Avito per-account ceiling', () => {
  const presentation = getListingLimitPresentation(null);

  assert.equal(presentation.total, 'Без общего лимита активных объявлений в MAP');
  assert.match(presentation.perAvitoAccount, /10\s000 в одном Avito-аккаунте/);
  assert.equal(presentation.requiresMultipleAvitoAccounts, true);
});
