import assert from 'node:assert/strict';
import test from 'node:test';

import type {
  OzonCatalogTreeLevel,
  OzonCatalogTreeOption,
} from '../src/lib/marketplace-account-types';
import {
  normalizeOzonMargin,
  ozonCategoryPathIds,
  ozonEnabledOverride,
  ozonPolicyDraft,
  ozonPolicySourceLabel,
  ozonTreeLevelResponse,
} from '../src/lib/ozon-category-policy-ui';

function option(
  kind: 'category' | 'type',
  descriptionCategoryId: number,
  typeId: number | null,
): OzonCatalogTreeOption {
  return {
    kind,
    description_category_id: descriptionCategoryId,
    type_id: typeId,
    name: kind === 'category' ? 'Автозапчасти' : 'Шланг тормозной',
    category_path: 'Автотовары → Автозапчасти',
    policy: {
      enabled_override: null,
      effective_enabled: true,
      enabled_source: null,
      margin_pct: null,
      effective_margin_pct: '0',
      margin_source: null,
    },
  };
}

const level: OzonCatalogTreeLevel = {
  path: [
    { description_category_id: 101, name: 'Автотовары' },
    { description_category_id: 102, name: 'Автозапчасти' },
  ],
  options: [],
  tree_revision: 'f'.repeat(64),
};

test('Ozon category and type payload paths preserve the exact hierarchy', () => {
  assert.deepEqual(ozonCategoryPathIds(level, option('category', 103, null)), [101, 102, 103]);
  assert.deepEqual(ozonCategoryPathIds(level, option('type', 102, 202)), [101, 102]);
});

test('Ozon policy draft keeps explicit values separate from effective inheritance', () => {
  const inherited = option('type', 102, 202);
  inherited.policy.effective_enabled = false;
  inherited.policy.effective_margin_pct = '15.00';
  assert.deepEqual(ozonPolicyDraft(inherited), { enabled: 'inherit', margin: '' });

  inherited.policy.enabled_override = true;
  inherited.policy.margin_pct = '20.00';
  assert.deepEqual(ozonPolicyDraft(inherited), { enabled: 'enabled', margin: '20.00' });
  assert.equal(ozonEnabledOverride('inherit'), null);
  assert.equal(ozonEnabledOverride('enabled'), true);
  assert.equal(ozonEnabledOverride('disabled'), false);
});

test('Ozon margin accepts comma and inheritance, rejects unsafe precision and range', () => {
  assert.deepEqual(normalizeOzonMargin(''), { value: null, error: null });
  assert.deepEqual(normalizeOzonMargin(' 12,50 '), { value: '12.50', error: null });
  assert.equal(normalizeOzonMargin('12.345').value, null);
  assert.match(normalizeOzonMargin('12.345').error ?? '', /двумя знаками/);
  assert.match(normalizeOzonMargin('-100.01').error ?? '', /от −100%/);
  assert.match(normalizeOzonMargin('1000').error ?? '', /999,99%/);
});

test('Ozon policy source labels distinguish own, inherited and default values', () => {
  const leaf = option('type', 102, 202);
  assert.equal(ozonPolicySourceLabel(null, leaf, '0% по умолчанию'), '0% по умолчанию');
  assert.equal(ozonPolicySourceLabel({
    description_category_id: 102,
    type_id: 202,
    name: 'Шланг тормозной',
    category_path: 'Автотовары → Автозапчасти',
  }, leaf, 'fallback'), 'задано здесь');
  assert.equal(ozonPolicySourceLabel({
    description_category_id: 101,
    type_id: null,
    name: 'Автотовары',
    category_path: 'Автотовары',
  }, leaf, 'fallback'), 'наследуется от «Автотовары»');
});

test('Ozon tree level unwraps only a structurally valid local response', () => {
  const response = { status: 'ok', data: level };
  assert.equal(ozonTreeLevelResponse(response), level);
  assert.throws(() => ozonTreeLevelResponse({ status: 'ok', data: {} }), /Invalid local/);
});
