import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  authorization,
  failure,
  installBrowserEnvironment,
  requestBody,
  response,
} from './browser-test-helpers';


test('concurrent 401 responses share one refresh and replay in the same session', async () => {
  installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const {
    authApi,
    default: api,
    getAccessToken,
    getBrowserSessionRevision,
  } = await import('../src/lib/api');
  handler = async (config) => {
    if (config.url === '/auth/browser/csrf/') {
      return response(config, { csrf_token: 'csrf-token' });
    }
    if (config.url === '/auth/browser/login/') {
      assert.equal(config.headers.get('X-CSRFToken'), 'csrf-token');
      assert.deepEqual(requestBody(config), {
        email: 'owner@example.test',
        password: 'correct-password',
        tenant_slug: 'tenant-a',
      });
      return response(config, {
        access: 'access-a',
        browser_session_id: 'session-a-1234567890',
      });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  const login = await authApi.login(
    'owner@example.test',
    'correct-password',
    'tenant-a',
  );
  assert.equal(login.browserSessionRevision, 1);
  assert.equal(getBrowserSessionRevision(), 1);
  assert.equal(getAccessToken(), 'access-a');

  let refreshCalls = 0;
  let protectedCalls = 0;
  handler = async (config) => {
    if (config.url === '/auth/browser/refresh/') {
      refreshCalls += 1;
      return response(config, {
        access: 'access-a-refreshed',
        browser_session_id: 'session-a-1234567890',
      });
    }
    if (config.url === '/protected/') {
      protectedCalls += 1;
      if (authorization(config) === 'Bearer access-a-refreshed') {
        return response(config, { ok: true });
      }
      throw failure(config, 401, { code: 'token_not_valid' });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  const protectedResponses = await Promise.all([
    api.get('/protected/'),
    api.get('/protected/'),
  ]);
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 4);
  assert.deepEqual(protectedResponses.map(({ data }) => data), [
    { ok: true },
    { ok: true },
  ]);
  assert.equal(getAccessToken(), 'access-a-refreshed');
  assert.equal(getBrowserSessionRevision(), 1);
});
