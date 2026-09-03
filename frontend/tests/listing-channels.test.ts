import assert from 'node:assert/strict';
import test from 'node:test';

import {
  hasLegacyListingActions,
  listingChannelDrawerHref,
  listingChannelKey,
  type ListingChannel,
} from '../src/lib/listing-channels';

function channel(overrides: Partial<ListingChannel>): ListingChannel {
  return {
    id: 41,
    resource_id: 41,
    channel_id: 'listing:41',
    resource_kind: 'listing',
    product_id: 3212,
    account_id: 4,
    status: 'active',
    status_display: 'Активно',
    status_explanation: '',
    delivery_stage: '',
    delivery_retry_at: null,
    delivery_retry_reason: '',
    provider_submission_started: true,
    lifecycle_actions_blocked: false,
    can_check_avito_status: true,
    can_check_provider_status: true,
    can_publish: false,
    rejection_ready_to_retry: false,
    product_article: '35011068',
    product_name: 'Шланг тормозной',
    account_name: 'АльфаПроГрупп',
    marketplace: 'avito',
    marketplace_label: 'Avito',
    title: 'Шланг тормозной',
    price_on_listing: '650.84',
    external_url: '',
    rejection_reason: '',
    retry_count: 0,
    published_at: '2026-09-03T16:27:00Z',
    last_sync_at: '2026-09-03T16:27:00Z',
    remote_status: 'active',
    remote_status_checked_at: '2026-09-03T16:27:00Z',
    next_status_check_at: null,
    provider_sku: null,
    provider_product_id: null,
    created_at: '2026-09-03T16:20:00Z',
    ...overrides,
  };
}

test('legacy listing keeps the protected Avito drawer route and lifecycle actions', () => {
  const listing = channel({});

  assert.equal(hasLegacyListingActions(listing), true);
  assert.equal(listingChannelKey(listing), 'listing:41');
  assert.equal(
    listingChannelDrawerHref(
      '/dashboard/listings',
      'status=active&product=3212&target=5&panel=pricing',
      listing,
    ),
    '/dashboard/listings?status=active&listing=41',
  );
});

test('Ozon offer opens its exact product/account workspace without legacy actions', () => {
  const offer = channel({
    id: 7,
    resource_id: 7,
    channel_id: 'ozon_offer:7',
    resource_kind: 'ozon_offer',
    account_id: 5,
    marketplace: 'ozon',
    marketplace_label: 'Ozon',
    provider_sku: 5692456653,
    can_check_avito_status: false,
    can_check_provider_status: false,
  });

  assert.equal(hasLegacyListingActions(offer), false);
  assert.equal(listingChannelKey(offer), 'ozon_offer:7');
  assert.equal(
    listingChannelDrawerHref(
      '/dashboard/listings',
      'marketplace=ozon&page=2&listing=41&panel=pricing',
      offer,
    ),
    '/dashboard/listings?marketplace=ozon&page=2&product=3212&target=5',
  );
});
