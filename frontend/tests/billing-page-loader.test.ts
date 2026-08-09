import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canStartBillingMutation,
  loadBillingPageData,
} from '../src/lib/billing-page-loader';


test('billing mutations stay blocked until subscription state is trustworthy', () => {
  assert.equal(canStartBillingMutation('loading'), false);
  assert.equal(canStartBillingMutation('error'), false);
  assert.equal(canStartBillingMutation('loaded'), true);
});


test('billing plans become available without waiting for subscription or optional endpoints', async () => {
  let releaseBlocked!: () => void;
  const blockedGate = new Promise<void>((resolve) => {
    releaseBlocked = resolve;
  });
  const load = loadBillingPageData({
    subscription: async () => {
      await blockedGate;
      return { plan: 'pro' };
    },
    plans: async () => ['free', 'pro'],
    invoices: async () => {
      await blockedGate;
      return [];
    },
    usage: async () => {
      await blockedGate;
      return { used: 0 };
    },
    packages: async () => {
      await blockedGate;
      return [];
    },
  });

  let subscriptionSettled = false;
  void load.subscription.then(() => {
    subscriptionSettled = true;
  });
  const plans = await load.plans;

  assert.equal(plans.status, 'fulfilled');
  assert.equal(subscriptionSettled, false);

  releaseBlocked();
  await Promise.all([
    load.subscription,
    load.invoices,
    load.usage,
    load.packages,
  ]);
});


test('an optional billing failure is isolated from checkout-critical data', async () => {
  const load = loadBillingPageData({
    subscription: async () => ({ plan: 'pro' }),
    plans: async () => ['free', 'pro'],
    invoices: async () => Promise.reject(new Error('invoices unavailable')),
    usage: async () => ({ used: 1 }),
    packages: async () => [],
  });

  const [subscription, plans, invoices, usage, packages] = await Promise.all([
    load.subscription,
    load.plans,
    load.invoices,
    load.usage,
    load.packages,
  ]);

  assert.equal(subscription.status, 'fulfilled');
  assert.equal(plans.status, 'fulfilled');
  assert.equal(invoices.status, 'rejected');
  assert.equal(usage.status, 'fulfilled');
  assert.equal(packages.status, 'fulfilled');
});
