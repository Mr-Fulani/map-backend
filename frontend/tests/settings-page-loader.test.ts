import assert from 'node:assert/strict';
import test from 'node:test';

import {
  claimSettingsLoadGroups,
  settingsLoadGroups,
  type SettingsLoadGroup,
} from '../src/lib/settings-page-loader';

test('settings only load data required by the visible tab', () => {
  assert.deepEqual(settingsLoadGroups('profile'), []);
  assert.deepEqual(settingsLoadGroups('organization'), []);
  assert.deepEqual(settingsLoadGroups('web-research'), []);
  assert.deepEqual(settingsLoadGroups('marketplaces'), ['marketplaces']);
  assert.deepEqual(settingsLoadGroups('ozon-categories'), ['marketplaces']);
  assert.deepEqual(settingsLoadGroups('ozon-pricing'), ['marketplaces']);
  assert.deepEqual(
    settingsLoadGroups('catalog-categories'),
    ['catalog-domains', 'catalog'],
  );
  assert.deepEqual(settingsLoadGroups('pricing'), ['pricing-categories']);
});

test('a settings data group is claimed only once per page mount', () => {
  const claimed = new Set<SettingsLoadGroup>();

  assert.deepEqual(claimSettingsLoadGroups('marketplaces', claimed), ['marketplaces']);
  assert.deepEqual(claimSettingsLoadGroups('marketplaces', claimed), []);
  assert.deepEqual(
    claimSettingsLoadGroups('catalog-categories', claimed),
    ['catalog-domains', 'catalog'],
  );
  assert.deepEqual(claimSettingsLoadGroups('catalog-categories', claimed), []);
});
