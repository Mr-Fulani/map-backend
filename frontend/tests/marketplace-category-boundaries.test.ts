import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AVITO_PRICING_LABEL,
  canEditMapCatalogStructure,
  MAP_CATALOG_LABEL,
  mapCatalogCategorySourceLabel,
  MARKETPLACE_CATEGORIES_LABEL,
  MARKETPLACE_PRICING_LABEL,
  OZON_CATEGORIES_LABEL,
  OZON_PRICING_LABEL,
} from '../src/lib/marketplace-category-boundaries';

test('tenant-facing labels separate the product catalog from marketplace settings', () => {
  assert.equal(MAP_CATALOG_LABEL, 'Каталог товаров');
  assert.equal(MARKETPLACE_CATEGORIES_LABEL, 'Категории площадок');
  assert.equal(MARKETPLACE_PRICING_LABEL, 'Правила цены');
  assert.equal(AVITO_PRICING_LABEL, 'Правила цены Avito');
  assert.equal(OZON_CATEGORIES_LABEL, 'Справочник категорий Ozon');
  assert.equal(OZON_PRICING_LABEL, 'Правила цены Ozon');
  assert.notEqual(MAP_CATALOG_LABEL, AVITO_PRICING_LABEL);
  assert.notEqual(OZON_CATEGORIES_LABEL, OZON_PRICING_LABEL);
});

test('official Avito branches remain identifiable and structure-protected', () => {
  assert.match(mapCatalogCategorySourceLabel('avito'), /Avito/);
  assert.equal(canEditMapCatalogStructure('avito'), false);
  assert.equal(canEditMapCatalogStructure(''), true);
  assert.equal(canEditMapCatalogStructure('tenant'), true);
});
