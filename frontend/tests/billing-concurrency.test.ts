import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  failure,
  installBrowserEnvironment,
  requestBody,
  response,
} from './browser-test-helpers';


test('concurrent checkout reuses one key and rotates only after terminal intent', async () => {
  const { localStorage } = installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const {
    authApi,
    billingApi,
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

  const billingKeys: string[] = [];
  let billingOutcome: 'server-error' | 'terminal' | 'success' = 'server-error';
  handler = async (config) => {
    if (config.url !== '/billing/checkout/') {
      throw new Error(`Unexpected request: ${config.url}`);
    }
    const body = requestBody(config) as { idempotency_key: string };
    billingKeys.push(body.idempotency_key);
    if (billingOutcome === 'server-error') {
      throw failure(config, 503, { code: 'provider_unavailable' });
    }
    if (billingOutcome === 'terminal') {
      throw failure(config, 409, {
        code: 'checkout_terminal',
        data: { rotate_idempotency_key: true },
      });
    }
    return response(config, { status: 'pending' });
  };

  const concurrentCheckout = await Promise.allSettled([
    billingApi.checkout('pro', 'monthly'),
    billingApi.checkout('pro', 'monthly'),
  ]);
  assert.deepEqual(
    concurrentCheckout.map(({ status }) => status),
    ['rejected', 'rejected'],
  );
  assert.equal(billingKeys[0], billingKeys[1]);

  billingOutcome = 'terminal';
  await assert.rejects(billingApi.checkout('pro', 'monthly'));
  assert.equal(billingKeys[2], billingKeys[0]);

  billingOutcome = 'success';
  await billingApi.checkout('pro', 'monthly');
  assert.notEqual(billingKeys[3], billingKeys[0]);
  assert.match(
    billingKeys[3],
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  );

  handler = async (config) => {
    if (config.url === '/auth/browser/login/') {
      return response(config, {
        access: 'access-b',
        browser_session_id: 'session-b-1234567890',
      });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };
  await authApi.login('second@example.test', 'password', 'tenant-b');
  assert.equal(getBrowserSessionRevision(), 2);
  assert.equal(
    Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .some((key) => key?.startsWith('map:billing-attempt:')),
    false,
  );
});
