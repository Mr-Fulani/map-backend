import assert from 'node:assert/strict';
import test from 'node:test';

import {
  firstPublicationErrorField,
  hasPublicationFieldErrors,
  publicationActionLabel,
  publicationFieldErrorsFromApi,
} from '../src/lib/listing-publication';

test('publication errors focus the first editable field in drawer order', () => {
  const errors = {
    contact_phone_override: ['Укажите телефон'],
    product_brand: ['Исправьте бренд'],
  };

  assert.equal(firstPublicationErrorField(errors), 'product_brand');
  assert.equal(hasPublicationFieldErrors(errors), true);
  assert.equal(hasPublicationFieldErrors({}), false);
});

test('terminal pre-submission failure has an explicit retry label', () => {
  assert.equal(
    publicationActionLabel('delivery_failed'),
    'Исправить и отправить снова',
  );
  assert.equal(publicationActionLabel('draft'), 'Опубликовать');
});

test('backend field errors and DRF serializer errors share one drawer contract', () => {
  assert.deepEqual(
    publicationFieldErrorsFromApi({
      field_errors: { product_brand: ['Исправьте бренд'] },
    }),
    { product_brand: ['Исправьте бренд'] },
  );
  assert.deepEqual(
    publicationFieldErrorsFromApi({ price_on_listing: ['Цена должна быть положительной'] }),
    { price_on_listing: ['Цена должна быть положительной'] },
  );
});
