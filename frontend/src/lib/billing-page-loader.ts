export interface BillingPageRequests<
  TSubscription,
  TPlans,
  TInvoices,
  TUsage,
  TPackages,
> {
  subscription: () => Promise<TSubscription>;
  plans: () => Promise<TPlans>;
  invoices: () => Promise<TInvoices>;
  usage: () => Promise<TUsage>;
  packages: () => Promise<TPackages>;
}

export type BillingLoadState = 'loading' | 'loaded' | 'error';

export function canStartBillingMutation(
  subscriptionState: BillingLoadState,
  billingEnabled = true,
): boolean {
  return billingEnabled && subscriptionState === 'loaded';
}

function settle<T>(
  request: () => Promise<T>,
): Promise<PromiseSettledResult<T>> {
  let started: Promise<T>;
  try {
    started = Promise.resolve(request());
  } catch (error) {
    started = Promise.reject(error);
  }
  return started.then(
    (value) => ({ status: 'fulfilled', value }),
    (reason: unknown) => ({ status: 'rejected', reason }),
  );
}

/**
 * Starts every billing request immediately and settles each independently.
 * A slow subscription, invoice, usage or package endpoint must never hide
 * already available plans or any other successful section.
 */
export function loadBillingPageData<
  TSubscription,
  TPlans,
  TInvoices,
  TUsage,
  TPackages,
>(
  requests: BillingPageRequests<
    TSubscription,
    TPlans,
    TInvoices,
    TUsage,
    TPackages
  >,
) {
  return {
    subscription: settle(requests.subscription),
    plans: settle(requests.plans),
    invoices: settle(requests.invoices),
    usage: settle(requests.usage),
    packages: settle(requests.packages),
  };
}
