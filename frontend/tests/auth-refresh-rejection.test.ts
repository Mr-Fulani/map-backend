import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  failure,
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('a rejected refresh clears the access token and never replays the request', async () => {
  const { localStorage } = installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const {
    authApi,
    default: api,
    getAccessToken,
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

  let protectedCalls = 0;
  let refreshCalls = 0;
  let logoutCalls = 0;
  handler = async (config) => {
    if (config.url === '/protected/') {
      protectedCalls += 1;
      throw failure(config, 401, { code: 'token_not_valid' });
    }
    if (config.url === '/auth/browser/refresh/') {
      refreshCalls += 1;
      throw failure(config, 401, { code: 'token_not_valid' });
    }
    if (config.url === '/auth/browser/logout/') {
      logoutCalls += 1;
      return response(config, {});
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  await assert.rejects(
    api.get('/protected/'),
    (error: unknown) => axios.isAxiosError(error) && error.response?.status === 401,
  );

  assert.equal(protectedCalls, 1);
  assert.equal(refreshCalls, 1);
  assert.equal(logoutCalls, 1);
  assert.equal(getAccessToken(), null);
  assert.equal(
    JSON.parse(localStorage.getItem('map:browser-session-version') ?? '{}').state,
    'cleared',
  );
});
