import assert from 'node:assert/strict';
import test from 'node:test';

import {
  resolveSettingsHash,
  settingsHash,
} from '../src/lib/settings-marketplace-navigation';

test('legacy settings links open the equivalent marketplace view', () => {
  assert.deepEqual(resolveSettingsHash('accounts'), { tab: 'marketplaces' });
  assert.deepEqual(resolveSettingsHash('ozon-categories'), {
    tab: 'marketplace-categories',
    categoryProvider: 'ozon',
  });
  assert.deepEqual(resolveSettingsHash('pricing'), {
    tab: 'marketplace-pricing',
    pricingProvider: 'avito',
  });
  assert.deepEqual(resolveSettingsHash('ozon-pricing'), {
    tab: 'marketplace-pricing',
    pricingProvider: 'ozon',
  });
});

test('canonical marketplace links preserve the selected provider', () => {
  assert.deepEqual(resolveSettingsHash('marketplace-categories-avito'), {
    tab: 'marketplace-categories',
    categoryProvider: 'avito',
  });
  assert.deepEqual(resolveSettingsHash('marketplace-pricing-ozon'), {
    tab: 'marketplace-pricing',
    pricingProvider: 'ozon',
  });
  assert.equal(
    settingsHash('marketplace-categories', 'ozon', 'avito'),
    'marketplace-categories-ozon',
  );
  assert.equal(
    settingsHash('marketplace-pricing', 'avito', 'ozon'),
    'marketplace-pricing-ozon',
  );
});

test('ordinary settings links remain unchanged', () => {
  assert.deepEqual(resolveSettingsHash('catalog-categories'), {
    tab: 'catalog-categories',
  });
  assert.equal(settingsHash('notifications', 'avito', 'ozon'), 'notifications');
});
