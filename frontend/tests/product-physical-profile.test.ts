import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canonicalPhysicalValueToDisplay,
  effectivePhysicalValueForInput,
  physicalDraftFromProfile,
  physicalDraftToApiPayload,
  physicalSuggestionIsAlreadyUsed,
  physicalSuggestionNeedsReview,
  PRODUCT_PHYSICAL_GUIDANCE,
  type ProductPhysicalProfile,
  type ProductPhysicalSuggestion,
} from '../src/lib/product-physical-profile';


const profile: ProductPhysicalProfile = {
  facts: {
    barcode: {
      source_value: 'SOURCE-CODE',
      map_value: 'MAP-CODE',
      effective_value: 'SOURCE-CODE',
      effective_source: '1c',
      source_error: '',
      map_provenance: null,
    },
    length_mm: {
      source_value: null,
      map_value: '255',
      effective_value: '255',
      effective_source: 'map',
      source_error: '',
      map_provenance: null,
    },
    width_mm: {
      source_value: null,
      map_value: null,
      effective_value: null,
      effective_source: 'missing',
      source_error: 'Некорректное число.',
      map_provenance: null,
    },
    height_mm: {
      source_value: null,
      map_value: '80',
      effective_value: '80',
      effective_source: 'map',
      source_error: '',
      map_provenance: null,
    },
    weight_g: {
      source_value: null,
      map_value: '1250',
      effective_value: '1250',
      effective_source: 'map',
      source_error: '',
      map_provenance: null,
    },
    vat_rate: {
      source_value: '20',
      map_value: '10',
      effective_value: '20',
      effective_source: '1c',
      source_error: '',
      map_provenance: null,
    },
  },
  suggestions: [],
  units: { dimensions: 'mm', weight: 'g', vat: 'percent' },
  complete: false,
  missing_fields: ['width_mm'],
  source_updated_at: '2026-08-30T01:00:00Z',
  updated_at: '2026-08-30T01:00:00Z',
};


test('shows canonical backend units as cm and kg for users', () => {
  assert.equal(canonicalPhysicalValueToDisplay('length_mm', '255'), '25,5');
  assert.equal(canonicalPhysicalValueToDisplay('weight_g', '1250'), '1,25');
  assert.equal(canonicalPhysicalValueToDisplay('vat_rate', '20'), '20');
});

test('keeps MAP fallback separate while displaying a valid 1C value', () => {
  const draft = physicalDraftFromProfile(profile);
  assert.equal(draft.barcode, 'MAP-CODE');
  assert.equal(draft.length_mm, '25,5');
  assert.equal(effectivePhysicalValueForInput(profile, 'barcode', draft), 'SOURCE-CODE');
  assert.equal(effectivePhysicalValueForInput(profile, 'length_mm', draft), '25,5');
});

test('converts user-facing cm and kg back to canonical API units', () => {
  const payload = physicalDraftToApiPayload({
    barcode: '  4650252914394 ',
    length_mm: '25,5',
    width_mm: '12',
    height_mm: '',
    weight_g: '1,25',
    vat_rate: '7',
  });
  assert.deepEqual(payload, {
    barcode: '4650252914394',
    length_mm: '255',
    width_mm: '120',
    height_mm: null,
    weight_g: '1250',
    vat_rate: '7',
  });
});

test('rejects unsupported VAT before sending the request', () => {
  assert.throws(
    () => physicalDraftToApiPayload({
      barcode: '',
      length_mm: '',
      width_mm: '',
      height_mm: '',
      weight_g: '',
      vat_rate: '18',
    }),
    /НДС/,
  );
  assert.throws(
    () => physicalDraftToApiPayload({
      barcode: '12345678', length_mm: '', width_mm: '', height_mm: '',
      weight_g: '', vat_rate: '',
    }),
    /EAN\/GTIN/,
  );
});

test('physical fields tell the tenant where to get authoritative values', () => {
  assert.match(PRODUCT_PHYSICAL_GUIDANCE.barcode.source, /с упаковки/);
  assert.match(PRODUCT_PHYSICAL_GUIDANCE.barcode.warning, /оставьте поле пустым/);
  assert.match(PRODUCT_PHYSICAL_GUIDANCE.length_mm.warning, /размер упаковки/);
  assert.match(PRODUCT_PHYSICAL_GUIDANCE.weight_g.source, /вместе с упаковкой/);
  assert.match(PRODUCT_PHYSICAL_GUIDANCE.vat_rate.source, /бухгалтера/);
});

test('does not ask to approve an enrichment value already used by MAP', () => {
  const suggestion: ProductPhysicalSuggestion = {
    id: 42,
    field: 'length_mm',
    value: '255',
    source_id: 'tachka',
    source_label: 'Tachka.ru',
    source_url: 'https://example.test/product',
    raw_name: 'Длина упаковки',
    raw_value: '255 мм',
    confidence: 0.95,
    review_status: 'pending',
    last_seen_at: '2026-08-31T12:00:00Z',
  };

  assert.equal(physicalSuggestionIsAlreadyUsed(profile, suggestion), true);
  assert.equal(physicalSuggestionNeedsReview(profile, suggestion), false);
});

test('asks for review only when an enrichment value is still undecided', () => {
  const suggestion: ProductPhysicalSuggestion = {
    id: 43,
    field: 'width_mm',
    value: '120',
    source_id: 'tachka',
    source_label: 'Tachka.ru',
    source_url: 'https://example.test/product',
    raw_name: 'Ширина упаковки',
    raw_value: '120 мм',
    confidence: 0.95,
    review_status: 'pending',
    last_seen_at: '2026-08-31T12:00:00Z',
  };

  assert.equal(physicalSuggestionIsAlreadyUsed(profile, suggestion), false);
  assert.equal(physicalSuggestionNeedsReview(profile, suggestion), true);
  assert.equal(
    physicalSuggestionNeedsReview(profile, { ...suggestion, review_status: 'rejected' }),
    false,
  );
});
