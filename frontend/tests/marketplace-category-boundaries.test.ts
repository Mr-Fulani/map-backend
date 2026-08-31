import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AVITO_PRICING_LABEL,
  canEditMapCatalogStructure,
  MAP_CATALOG_LABEL,
  mapCatalogCategorySourceLabel,
  OZON_CATEGORIES_LABEL,
  OZON_PRICING_LABEL,
} from '../src/lib/marketplace-category-boundaries';

test('tenant-facing labels keep MAP catalog and Avito pricing distinct', () => {
  assert.equal(MAP_CATALOG_LABEL, 'Каталог MAP');
  assert.equal(AVITO_PRICING_LABEL, 'Наценки Avito');
  assert.equal(OZON_CATEGORIES_LABEL, 'Категории Ozon');
  assert.equal(OZON_PRICING_LABEL, 'Наценки Ozon');
  assert.notEqual(MAP_CATALOG_LABEL, AVITO_PRICING_LABEL);
  assert.notEqual(OZON_CATEGORIES_LABEL, OZON_PRICING_LABEL);
});

test('official Avito branches remain identifiable and structure-protected', () => {
  assert.match(mapCatalogCategorySourceLabel('avito'), /Avito/);
  assert.equal(canEditMapCatalogStructure('avito'), false);
  assert.equal(canEditMapCatalogStructure(''), true);
  assert.equal(canEditMapCatalogStructure('tenant'), true);
});
