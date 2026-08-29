import assert from 'node:assert/strict';
import test from 'node:test';

import {
  firstPublicationErrorField,
  firstPublicationWarningField,
  hasPublicationFieldErrors,
  hasPublicationFieldWarnings,
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

test('non-blocking publication warnings use the same field order', () => {
  const warnings = {
    product_brand: ['Необязательный бренд не будет отправлен'],
    product_oem: ['Несколько OEM не будут отправлены'],
    images: ['Добавьте фотографию товара'],
  };

  assert.equal(firstPublicationWarningField(warnings), 'images');
  assert.equal(hasPublicationFieldWarnings(warnings), true);
  assert.equal(hasPublicationFieldWarnings({}), false);
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

test('product OEM serializer errors highlight the OEM drawer field', () => {
  assert.deepEqual(publicationFieldErrorsFromApi({
    avito_oem: ['OEM содержит недопустимые символы.'],
  }), {
    product_oem: ['OEM содержит недопустимые символы.'],
  });
});
