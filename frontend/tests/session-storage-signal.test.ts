import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('a newer storage session signal clears credentials and billing attempts', async () => {
  const browser = installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const {
    authApi,
    getAccessToken,
    getBrowserSessionRevision,
  } = await import('../src/lib/api');
  handler = async (config) => {
    if (config.url === '/auth/browser/csrf/') {
      return response(config, { csrf_token: 'csrf-token' });
    }
    if (config.url === '/auth/browser/login/') {
      return response(config, {
        access: 'access-a',
        browser_session_id: 'session-a-1234567890',
      });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };
  await authApi.login('owner@example.test', 'correct-password', 'tenant-a');

  const billingKey = 'map:billing-attempt:pending-checkout';
  browser.localStorage.setItem(
    billingKey,
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  );
  const replacement = JSON.stringify({
    revision: 2,
    sequence: 2,
    state: 'active',
    sessionId: 'session-b-1234567890',
  });
  browser.localStorage.setItem('map:browser-session-version', replacement);
  browser.dispatchStorageEvent('map:browser-session-version', replacement);

  assert.equal(getBrowserSessionRevision(), 2);
  assert.equal(getAccessToken(), null);
  assert.equal(browser.localStorage.getItem(billingKey), null);
});
