import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AVITO_PRICING_LABEL,
  canEditMapCatalogStructure,
  MAP_CATALOG_LABEL,
  mapCatalogCategorySourceLabel,
} from '../src/lib/marketplace-category-boundaries';

test('tenant-facing labels keep MAP catalog and Avito pricing distinct', () => {
  assert.equal(MAP_CATALOG_LABEL, 'Каталог MAP');
  assert.equal(AVITO_PRICING_LABEL, 'Наценки Avito');
  assert.notEqual(MAP_CATALOG_LABEL, AVITO_PRICING_LABEL);
});

test('official Avito branches remain identifiable and structure-protected', () => {
  assert.match(mapCatalogCategorySourceLabel('avito'), /Avito/);
  assert.equal(canEditMapCatalogStructure('avito'), false);
  assert.equal(canEditMapCatalogStructure(''), true);
  assert.equal(canEditMapCatalogStructure('tenant'), true);
});
