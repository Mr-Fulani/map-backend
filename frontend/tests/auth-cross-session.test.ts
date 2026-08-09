import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { type AxiosAdapter } from 'axios';

import {
  failure,
  installBrowserEnvironment,
  response,
} from './browser-test-helpers';


test('refreshing into a different cookie session blocks request replay', async () => {
  installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const {
    BrowserSessionChangedError,
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
      const isSecondLogin = getBrowserSessionRevision() === 1;
      return response(config, {
        access: isSecondLogin ? 'access-b' : 'access-a',
        browser_session_id: isSecondLogin
          ? 'session-b-1234567890'
          : 'session-a-1234567890',
      });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };
  await authApi.login('owner@example.test', 'correct-password', 'tenant-a');
  await authApi.login('second@example.test', 'password', 'tenant-b');
  assert.equal(getBrowserSessionRevision(), 2);
  assert.equal(getAccessToken(), 'access-b');

  let crossSessionRequestCalls = 0;
  handler = async (config) => {
    if (config.url === '/cross-session/') {
      crossSessionRequestCalls += 1;
      throw failure(config, 401, { code: 'token_not_valid' });
    }
    if (config.url === '/auth/browser/refresh/') {
      return response(config, {
        access: 'access-c',
        browser_session_id: 'session-c-1234567890',
      });
    }
    throw new Error(`Unexpected request: ${config.url}`);
  };

  await assert.rejects(
    api.get('/cross-session/'),
    (error: unknown) => error instanceof BrowserSessionChangedError,
  );
  assert.equal(crossSessionRequestCalls, 1);
  assert.equal(getBrowserSessionRevision(), 3);
  assert.equal(getAccessToken(), 'access-c');
});
