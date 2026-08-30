import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canonicalPhysicalValueToDisplay,
  effectivePhysicalValueForInput,
  physicalDraftFromProfile,
  physicalDraftToApiPayload,
  type ProductPhysicalProfile,
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
    barcode: '  4601234567890 ',
    length_mm: '25,5',
    width_mm: '12',
    height_mm: '',
    weight_g: '1,25',
    vat_rate: '7',
  });
  assert.deepEqual(payload, {
    barcode: '4601234567890',
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
});
