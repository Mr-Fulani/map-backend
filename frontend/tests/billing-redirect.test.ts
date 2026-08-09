import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  failure,
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('subscription-inactive 402 replaces a non-billing dashboard location', async () => {
  const browser = installBrowserEnvironment('/dashboard/products');
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const { default: api } = await import('../src/lib/api');
  handler = async (config) => {
    if (config.url === '/billing/subscription/') {
      throw failure(config, 402, { code: 'subscription_inactive' });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  await assert.rejects(
    api.get('/billing/subscription/'),
    (error: unknown) => axios.isAxiosError(error) && error.response?.status === 402,
  );
  assert.equal(
    browser.replacedLocation(),
    '/dashboard/billing?subscription=inactive',
  );
});
