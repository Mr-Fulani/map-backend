import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  failure,
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('an auth POST retries a CSRF rejection exactly once with a fresh token', async () => {
  installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const { authApi } = await import('../src/lib/api');
  const csrfHeaders: string[] = [];
  let csrfCalls = 0;
  let loginCalls = 0;

  handler = async (config) => {
    if (config.url === '/auth/browser/csrf/') {
      csrfCalls += 1;
      return response(config, { csrf_token: `csrf-${csrfCalls}` });
    }
    if (config.url === '/auth/browser/login/') {
      loginCalls += 1;
      csrfHeaders.push(config.headers.get('X-CSRFToken')?.toString() ?? '');
      throw failure(config, 403, { code: 'csrf_failed' });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  await assert.rejects(
    authApi.login('owner@example.test', 'correct-password', 'tenant-a'),
    (error: unknown) => axios.isAxiosError(error) && error.response?.status === 403,
  );

  assert.equal(csrfCalls, 2);
  assert.equal(loginCalls, 2);
  assert.deepEqual(csrfHeaders, ['csrf-1', 'csrf-2']);
});
