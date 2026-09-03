export type ListingChannelResourceKind = 'listing' | 'ozon_offer';

export interface ListingChannel {
  id: number;
  resource_id: number;
  channel_id: string;
  resource_kind: ListingChannelResourceKind;
  product_id: number;
  account_id: number;
  status: string;
  status_display: string;
  status_explanation: string;
  delivery_stage: string;
  delivery_retry_at: string | null;
  delivery_retry_reason: string;
  provider_submission_started: boolean;
  lifecycle_actions_blocked: boolean;
  can_check_avito_status: boolean;
  can_check_provider_status: boolean;
  can_publish: boolean;
  rejection_ready_to_retry: boolean;
  product_article: string;
  product_name: string;
  account_name: string;
  marketplace: string;
  marketplace_label: string;
  title: string;
  price_on_listing: string;
  external_url: string;
  rejection_reason: string;
  retry_count: number;
  published_at: string | null;
  last_sync_at: string | null;
  remote_status: string | null;
  remote_status_checked_at: string | null;
  next_status_check_at: string | null;
  provider_sku: number | null;
  provider_product_id: number | null;
  created_at: string;
}

export type LegacyListingChannel = ListingChannel & { resource_kind: 'listing' };

export function hasLegacyListingActions(
  channel: ListingChannel,
): channel is LegacyListingChannel {
  return channel.resource_kind === 'listing';
}

export function listingChannelKey(channel: ListingChannel): string {
  return channel.channel_id;
}

export function listingChannelDrawerHref(
  pathname: string,
  currentSearch: string,
  channel: ListingChannel,
): string {
  const next = new URLSearchParams(currentSearch);
  next.delete('panel');

  if (hasLegacyListingActions(channel)) {
    next.set('listing', String(channel.resource_id));
    next.delete('product');
    next.delete('target');
  } else {
    next.set('product', String(channel.product_id));
    next.set('target', String(channel.account_id));
    next.delete('listing');
  }

  return next.size ? `${pathname}?${next.toString()}` : pathname;
}
