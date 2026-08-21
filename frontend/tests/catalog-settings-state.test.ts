import assert from 'node:assert/strict';
import test from 'node:test';

import {
  catalogCategoryBranchIds,
  updateCatalogCategoryBranch,
} from '../src/lib/catalog-settings-state';

const categories = [
  { id: 1, parent: null, is_active: true, name: 'root' },
  { id: 2, parent: 1, is_active: true, name: 'child' },
  { id: 3, parent: 2, is_active: true, name: 'grandchild' },
  { id: 4, parent: null, is_active: true, name: 'other' },
];

test('catalog branch ids include the full subtree only', () => {
  assert.deepEqual(
    [...catalogCategoryBranchIds(categories, 1)].sort((left, right) => left - right),
    [1, 2, 3],
  );
});

test('catalog branch state updates locally without changing other branches', () => {
  const updated = updateCatalogCategoryBranch(categories, 1, false);

  assert.deepEqual(
    updated.map(({ id, is_active }) => ({ id, is_active })),
    [
      { id: 1, is_active: false },
      { id: 2, is_active: false },
      { id: 3, is_active: false },
      { id: 4, is_active: true },
    ],
  );
  assert.equal(categories[0].is_active, true);
});
