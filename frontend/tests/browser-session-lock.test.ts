import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import { installBrowserEnvironment } from './browser-test-helpers';


const moduleRequire = createRequire(__filename);

function loadFreshLockModule(): typeof import('../src/lib/browser-session-lock') {
  const modulePath = moduleRequire.resolve('../src/lib/browser-session-lock');
  delete moduleRequire.cache[modulePath];
  return moduleRequire(modulePath) as typeof import('../src/lib/browser-session-lock');
}


test('browser session versions are validated and monotonic', async () => {
  const { localStorage } = installBrowserEnvironment();
  const {
    advanceBrowserSessionVersion,
    readBrowserSessionVersion,
    requireBrowserSessionVersion,
  } = await import('../src/lib/browser-session-lock');

  assert.deepEqual(readBrowserSessionVersion(), {
    revision: 0,
    sequence: 0,
    state: 'unknown',
    sessionId: null,
  });
  assert.throws(
    () => advanceBrowserSessionVersion(true, 'active', 'short'),
    /identifier is invalid/,
  );

  const active = advanceBrowserSessionVersion(
    true,
    'active',
    'session-a-1234567890',
  );
  const rotated = advanceBrowserSessionVersion(
    false,
    'active',
    'session-a-1234567890',
    active,
  );
  const cleared = advanceBrowserSessionVersion(true, 'cleared', null, rotated);

  assert.deepEqual(active, {
    revision: 1,
    sequence: 1,
    state: 'active',
    sessionId: 'session-a-1234567890',
  });
  assert.equal(rotated.revision, 1);
  assert.equal(rotated.sequence, 2);
  assert.equal(cleared.revision, 2);
  assert.equal(cleared.sequence, 3);
  assert.deepEqual(requireBrowserSessionVersion(), cleared);

  localStorage.setItem(
    'map:browser-session-version',
    JSON.stringify({
      revision: Number.MAX_SAFE_INTEGER + 1,
      sequence: 0,
      state: 'active',
      sessionId: 'session-a-1234567890',
    }),
  );
  assert.equal(readBrowserSessionVersion().state, 'unknown');
});


test('the in-realm queue serializes fallback-lock operations', async () => {
  installBrowserEnvironment();
  const { withBrowserSessionLock } = await import('../src/lib/browser-session-lock');
  let concurrent = 0;
  let maximumConcurrent = 0;

  await Promise.all(Array.from({ length: 4 }, (_, index) => (
    withBrowserSessionLock(async (guard) => {
      concurrent += 1;
      maximumConcurrent = Math.max(maximumConcurrent, concurrent);
      await new Promise((resolve) => setTimeout(resolve, 5 + index));
      guard.assertOwned();
      concurrent -= 1;
    })
  )));

  assert.equal(maximumConcurrent, 1);
  assert.equal(concurrent, 0);
});


test('independent module realms serialize through the shared storage fallback', async () => {
  installBrowserEnvironment();
  const firstRealm = loadFreshLockModule();
  const secondRealm = loadFreshLockModule();
  let concurrent = 0;
  let maximumConcurrent = 0;

  const operation = async (
    guard: { assertOwned: () => void },
    delay: number,
  ) => {
    concurrent += 1;
    maximumConcurrent = Math.max(maximumConcurrent, concurrent);
    await new Promise((resolve) => setTimeout(resolve, delay));
    guard.assertOwned();
    concurrent -= 1;
  };

  await Promise.all([
    firstRealm.withBrowserSessionLock((guard) => operation(guard, 30)),
    secondRealm.withBrowserSessionLock((guard) => operation(guard, 5)),
  ]);

  assert.equal(maximumConcurrent, 1);
  assert.equal(concurrent, 0);
});
