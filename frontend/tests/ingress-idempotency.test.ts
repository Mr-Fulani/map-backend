import assert from 'node:assert/strict';
import test from 'node:test';

import axios, { AxiosError, type AxiosAdapter } from 'axios';

import {
  failure,
  installBrowserEnvironment,
  requestBody,
  response,
} from './browser-test-helpers';


test('ingress attempts retain keys only while the response is ambiguous', async () => {
  const { localStorage } = installBrowserEnvironment();
  let handler: AxiosAdapter = async (config) => response(config, {});
  axios.defaults.adapter = (config) => handler(config);

  const { imageApi, productApi } = await import('../src/lib/api');
  const keys: string[] = [];
  let outcome: 'network' | 'http' | 'incomplete' | 'timeout' | 'server' | 'success' = 'network';
  handler = async (config) => {
    assert.equal(config.url, '/products/42/images/search/');
    const body = requestBody(config) as { idempotency_key: string };
    keys.push(body.idempotency_key);
    if (outcome === 'network') {
      throw new AxiosError('connection lost', 'ERR_NETWORK', config);
    }
    if (outcome === 'http') {
      throw failure(config, 409, { code: 'idempotency_conflict' });
    }
    if (outcome === 'incomplete') {
      throw failure(config, 409, { code: 'idempotency_incomplete' });
    }
    if (outcome === 'timeout') {
      throw failure(config, 408, { code: 'request_timeout' });
    }
    if (outcome === 'server') {
      throw failure(config, 503, { code: 'temporarily_unavailable' });
    }
    return response(config, { status: 'ok' });
  };

  await assert.rejects(imageApi.search(42));
  outcome = 'success';
  await imageApi.search(42);
  assert.equal(keys[1], keys[0]);

  outcome = 'http';
  await assert.rejects(imageApi.search(42));
  assert.equal(keys[2], keys[0]);
  outcome = 'success';
  await imageApi.search(42);
  assert.equal(keys[3], keys[2]);

  outcome = 'server';
  await assert.rejects(imageApi.search(42));
  outcome = 'timeout';
  await assert.rejects(imageApi.search(42));
  outcome = 'incomplete';
  await assert.rejects(imageApi.search(42));
  outcome = 'success';
  await imageApi.search(42);
  assert.deepEqual(keys, Array(keys.length).fill(keys[0]));

  const authoritativeKeys: string[] = [];
  let authoritative = true;
  handler = async (config) => {
    assert.equal(config.url, '/products/43/images/search/');
    const body = requestBody(config) as { idempotency_key: string };
    authoritativeKeys.push(body.idempotency_key);
    if (authoritative) {
      throw failure(config, 409, { code: 'idempotency_conflict' });
    }
    return response(config, { status: 'ok' });
  };
  await assert.rejects(imageApi.search(43));
  authoritative = false;
  await imageApi.search(43);
  assert.notEqual(authoritativeKeys[1], authoritativeKeys[0]);

  const explicitKeys: string[] = [];
  handler = async (config) => {
    assert.equal(config.url, '/products/parse/');
    const body = requestBody(config) as { idempotency_key: string };
    explicitKeys.push(body.idempotency_key);
    throw new AxiosError('connection lost', 'ERR_NETWORK', config);
  };
  const explicit = '40000000-0000-4000-8000-000000000001';
  await assert.rejects(productApi.parse(42, '', false, explicit));
  await assert.rejects(productApi.parse(42, '', false, explicit));
  assert.deepEqual(explicitKeys, [explicit, explicit]);

  const paidPaths = [
    {
      path: '/products/42/web-research/',
      call: () => productApi.startWebResearch(42),
    },
    {
      path: '/products/42/market-research/',
      call: async () => {
        const { webResearchApi } = await import('../src/lib/api');
        return webResearchApi.startMarketResearch(42);
      },
    },
    {
      path: '/listings/42/regenerate/',
      call: async () => {
        const { listingApi } = await import('../src/lib/api');
        return listingApi.regenerate(42);
      },
    },
  ];
  for (const paid of paidPaths) {
    const retained: string[] = [];
    let ambiguous = true;
    handler = async (config) => {
      assert.equal(config.url, paid.path);
      const body = requestBody(config) as { idempotency_key: string };
      retained.push(body.idempotency_key);
      if (ambiguous) throw new AxiosError('response lost', 'ERR_NETWORK', config);
      return response(config, { status: 'ok' });
    };
    await assert.rejects(paid.call());
    ambiguous = false;
    await paid.call();
    assert.equal(retained[1], retained[0], `${paid.path} must retain ambiguous key`);
  }

  assert.equal(
    Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .some((key) => key?.startsWith('map:ingress-attempt:')),
    true,
  );
});

test('concurrent success cannot rotate a key before an ambiguous sibling settles', async () => {
  installBrowserEnvironment();
  const seenKeys: string[] = [];
  let resolveSuccess: (() => void) | undefined;
  let rejectAmbiguous: (() => void) | undefined;
  let callCount = 0;
  const handler: AxiosAdapter = (config) => {
    callCount += 1;
    const body = requestBody(config) as { idempotency_key: string };
    seenKeys.push(body.idempotency_key);
    if (callCount === 1) {
      return new Promise((resolve) => {
        resolveSuccess = () => resolve(response(config, { status: 'ok' }));
      });
    }
    if (callCount === 2) {
      return new Promise((_resolve, reject) => {
        rejectAmbiguous = () => reject(
          new AxiosError('response lost', 'ERR_NETWORK', config),
        );
      });
    }
    return Promise.resolve(response(config, { status: 'ok' }));
  };
  const { default: api, imageApi } = await import('../src/lib/api');
  api.defaults.adapter = (config) => handler(config);
  const first = imageApi.search(84);
  const second = imageApi.search(84);
  while (!resolveSuccess || !rejectAmbiguous) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  resolveSuccess();
  await first;
  rejectAmbiguous();
  await assert.rejects(second);
  await imageApi.search(84);

  assert.deepEqual(seenKeys, [seenKeys[0], seenKeys[0], seenKeys[0]]);
});
