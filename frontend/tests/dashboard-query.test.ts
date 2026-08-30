import assert from 'node:assert/strict';
import test from 'node:test';

import {
  dashboardMarketplaceParam,
  dashboardPageParam,
  dashboardPositiveIdParam,
  dashboardQueryHref,
} from '../src/lib/dashboard-query';

test('dashboard query updates preserve unrelated parameters', () => {
  assert.equal(
    dashboardQueryHref(
      '/dashboard/listings',
      'listing=42&panel=pricing&status=active&page=3',
      { status: 'rejected', page: null },
    ),
    '/dashboard/listings?listing=42&panel=pricing&status=rejected',
  );
});

test('dashboard query deletes empty filters and validates pages', () => {
  assert.equal(
    dashboardQueryHref('/dashboard/logs', 'status=error&date=2026-08-13', {
      status: '',
      page: 2,
    }),
    '/dashboard/logs?date=2026-08-13&page=2',
  );
  assert.equal(dashboardPageParam('4'), 4);
  assert.equal(dashboardPageParam('0'), 1);
  assert.equal(dashboardPageParam('1.5'), 1);
  assert.equal(dashboardPageParam('nope'), 1);
});

test('marketplace and account filters are normalized and preserved in URLs', () => {
  assert.equal(dashboardMarketplaceParam('avito'), 'avito');
  assert.equal(dashboardMarketplaceParam('ozon'), 'ozon');
  assert.equal(dashboardMarketplaceParam('ozon', ['avito']), '');
  assert.equal(dashboardMarketplaceParam('ozon!'), '');

  assert.equal(dashboardPositiveIdParam('42'), 42);
  assert.equal(dashboardPositiveIdParam('0'), null);
  assert.equal(dashboardPositiveIdParam('-1'), null);
  assert.equal(dashboardPositiveIdParam('other'), null);

  assert.equal(
    dashboardQueryHref('/dashboard/listings', 'status=active&listing=7', {
      marketplace: 'ozon',
      account: 42,
      page: null,
    }),
    '/dashboard/listings?status=active&listing=7&marketplace=ozon&account=42',
  );
});
