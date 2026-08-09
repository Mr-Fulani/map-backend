import assert from 'node:assert/strict';
import test from 'node:test';

import axios, {
  AxiosError,
  type AxiosAdapter,
  type InternalAxiosRequestConfig,
} from 'axios';

import {
  failure,
  installBrowserEnvironment,
  requestBody,
  response,
} from './browser-test-helpers';


type FailureCase = {
  name: string;
  rotate: boolean;
  createError: (config: InternalAxiosRequestConfig) => Error;
};


const cases: FailureCase[] = [
  {
    name: 'terminal intent with explicit rotation flag',
    rotate: true,
    createError: (config) => failure(config, 409, {
      code: 'checkout_terminal',
      data: { rotate_idempotency_key: true },
    }),
  },
  {
    name: 'terminal intent without explicit rotation flag',
    rotate: false,
    createError: (config) => failure(config, 409, {
      code: 'checkout_terminal',
      data: { retryable: false },
    }),
  },
  {
    name: 'pending intent',
    rotate: false,
    createError: (config) => failure(config, 503, {
      code: 'checkout_pending',
      data: { retryable: true },
    }),
  },
  {
    name: 'manual-review intent',
    rotate: false,
    createError: (config) => failure(config, 409, {
      code: 'checkout_manual_review',
    }),
  },
  {
    name: 'checkout key limit',
    rotate: false,
    createError: (config) => failure(config, 409, {
      code: 'checkout_key_limit',
      data: { reuse_idempotency_key: true },
    }),
  },
  {
    name: 'idempotency conflict',
    rotate: false,
    createError: (config) => failure(config, 409, {
      code: 'idempotency_conflict',
    }),
  },
  {
    name: 'active paid subscription change is blocked',
    rotate: false,
    createError: (config) => failure(config, 409, {
      code: 'active_subscription_change_not_supported',
      data: { retryable: false },
    }),
  },
  {
    name: 'another subscription checkout is in progress',
    rotate: false,
    createError: (config) => failure(config, 409, {
      code: 'subscription_checkout_in_progress',
      data: { retryable: false, reuse_existing_checkout: true },
    }),
  },
  {
    name: 'generic 400 response',
    rotate: false,
    createError: (config) => failure(config, 400, { code: 'invalid_request' }),
  },
  {
    name: '408 response',
    rotate: false,
    createError: (config) => failure(config, 408, { code: 'timeout' }),
  },
  {
    name: '429 response',
    rotate: false,
    createError: (config) => failure(config, 429, { code: 'rate_limited' }),
  },
  {
    name: '5xx response',
    rotate: false,
    createError: (config) => failure(config, 502, { code: 'provider_error' }),
  },
  {
    name: 'network failure',
    rotate: false,
    createError: (config) => new AxiosError(
      'Network Error',
      'ERR_NETWORK',
      config,
    ),
  },
];


test('billing keys rotate only on the explicit terminal backend signal', async () => {
  installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const { authApi, billingApi } = await import('../src/lib/api');
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

  for (const [index, failureCase] of cases.entries()) {
    const observedKeys: string[] = [];
    let calls = 0;
    handler = async (config) => {
      if (config.url !== '/billing/ai-topup/') {
        throw new Error(`Unexpected request: ${config.url}`);
      }
      calls += 1;
      const body = requestBody(config) as { idempotency_key: string };
      observedKeys.push(body.idempotency_key);
      if (calls === 1) throw failureCase.createError(config);
      return response(config, { status: 'pending' });
    };

    await assert.rejects(
      billingApi.topupAI(index + 1),
      (error: unknown) => error instanceof Error,
      failureCase.name,
    );
    await billingApi.topupAI(index + 1);

    assert.equal(observedKeys.length, 2, failureCase.name);
    if (failureCase.rotate) {
      assert.notEqual(observedKeys[0], observedKeys[1], failureCase.name);
    } else {
      assert.equal(observedKeys[0], observedKeys[1], failureCase.name);
    }
  }
});
