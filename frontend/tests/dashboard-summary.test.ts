import assert from 'node:assert/strict';
import test from 'node:test';

import {
  dashboardAIBalanceState,
  dashboardNumber,
  dashboardPercent,
  hiddenTasksLabel,
  safeDashboardHref,
} from '../src/lib/dashboard-summary';

test('dashboard decimal values are normalized without hiding invalid values as zero', () => {
  assert.equal(dashboardNumber('106.5000'), 106.5);
  assert.equal(dashboardNumber(12), 12);
  assert.equal(dashboardNumber(''), null);
  assert.equal(dashboardNumber('not-a-number'), null);
  assert.equal(dashboardNumber(undefined), null);
});

test('dashboard usage percentage accepts decimal strings and remains bounded', () => {
  assert.equal(dashboardPercent('25', '100'), 25);
  assert.equal(dashboardPercent('120', '100'), 100);
  assert.equal(dashboardPercent('-5', '100'), 0);
  assert.equal(dashboardPercent('1', '0'), null);
  assert.equal(dashboardPercent('bad', '100'), null);
});

test('dashboard server-provided links stay inside the dashboard', () => {
  assert.equal(
    safeDashboardHref('/dashboard/listings?status=rejected#top'),
    '/dashboard/listings?status=rejected#top',
  );
  assert.equal(safeDashboardHref('/dashboard'), '/dashboard');
  assert.equal(safeDashboardHref('/login'), null);
  assert.equal(safeDashboardHref('https://example.com/dashboard'), null);
  assert.equal(safeDashboardHref('//example.com/dashboard'), null);
  assert.equal(safeDashboardHref('javascript:alert(1)'), null);
});

test('hidden attention task count uses correct Russian forms', () => {
  assert.equal(hiddenTasksLabel(1), 'Ещё 1 задача не показана');
  assert.equal(hiddenTasksLabel(2), 'Ещё 2 задачи не показаны');
  assert.equal(hiddenTasksLabel(5), 'Ещё 5 задач не показано');
  assert.equal(hiddenTasksLabel(11), 'Ещё 11 задач не показано');
  assert.equal(hiddenTasksLabel(21), 'Ещё 21 задача не показана');
});

test('AI resource state uses total available balance, not included-package threshold', () => {
  assert.equal(dashboardAIBalanceState({
    available_balance: '0', overage_active: false, threshold: 'exhausted', unlimited: false,
  }), 'exhausted');
  assert.equal(dashboardAIBalanceState({
    available_balance: '3', overage_active: true, threshold: 'exhausted', unlimited: false,
  }), 'purchased');
  assert.equal(dashboardAIBalanceState({
    available_balance: '20', overage_active: false, threshold: 'critical', unlimited: false,
  }), 'included_low');
  assert.equal(dashboardAIBalanceState({
    available_balance: '0', overage_active: false, threshold: 'exhausted', unlimited: true,
  }), 'unlimited');
});
