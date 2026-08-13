import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('billing and dashboard requests have bounded client deadlines', async () => {
  installBrowserEnvironment();
  const observed = new Map<string, number | undefined>();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const { authApi, billingApi, dashboardApi } = await import('../src/lib/api');
  handler = async (config) => {
    observed.set(config.url ?? '', config.timeout);
    if (config.url === '/auth/browser/csrf/') {
      return response(config, { csrf_token: 'csrf-token' });
    }
    if (config.url === '/auth/browser/login/') {
      return response(config, {
        access: 'access-a',
        browser_session_id: 'session-a-1234567890',
      });
    }
    if (config.url === '/billing/checkout/') {
      return response(config, { status: 'pending' });
    }
    return response(config, { data: [] });
  };

  await Promise.all([
    billingApi.getPlans(),
    billingApi.getSubscription(),
    billingApi.getUsage(),
    billingApi.getInvoices(),
    billingApi.getAIPackages(),
    dashboardApi.getSummary(),
  ]);
  for (const path of (
    [
      '/billing/plans/',
      '/billing/subscription/',
      '/billing/usage/',
      '/billing/invoices/',
      '/billing/ai-packages/',
    ]
  )) {
    assert.equal(observed.get(path), 10_000, path);
  }
  assert.equal(observed.get('/dashboard/summary/'), 15_000);

  await authApi.login('owner@example.test', 'password', 'tenant-a');
  await billingApi.checkout('pro', 'monthly');
  assert.equal(observed.get('/billing/checkout/'), 30_000);
});
