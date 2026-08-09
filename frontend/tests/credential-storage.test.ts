import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('access and refresh credentials are never persisted in localStorage', async () => {
  const { localStorage } = installBrowserEnvironment();
  localStorage.setItem('map_access_token', 'legacy-access');
  localStorage.setItem('map_refresh_token', 'legacy-refresh');

  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);
  const {
    authApi,
    clearLegacyTokenStorage,
    getAccessToken,
  } = await import('../src/lib/api');

  clearLegacyTokenStorage();
  handler = async (config) => {
    if (config.url === '/auth/browser/csrf/') {
      return response(config, { csrf_token: 'csrf-sensitive-token' });
    }
    if (config.url === '/auth/browser/login/') {
      return response(config, {
        access: 'access-sensitive-token',
        browser_session_id: 'session-a-1234567890',
      });
    }
    if (config.url === '/auth/browser/refresh/') {
      return response(config, {
        access: 'refreshed-sensitive-token',
        browser_session_id: 'session-a-1234567890',
      });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  await authApi.login('owner@example.test', 'correct-password', 'tenant-a');
  await authApi.refresh();

  assert.equal(getAccessToken(), 'refreshed-sensitive-token');
  assert.equal(localStorage.getItem('map_access_token'), null);
  assert.equal(localStorage.getItem('map_refresh_token'), null);
  const persistedValues = Array.from(
    { length: localStorage.length },
    (_, index) => localStorage.getItem(localStorage.key(index) ?? ''),
  );
  assert.equal(persistedValues.includes('access-sensitive-token'), false);
  assert.equal(persistedValues.includes('refreshed-sensitive-token'), false);
  assert.equal(persistedValues.includes('legacy-refresh'), false);
});
