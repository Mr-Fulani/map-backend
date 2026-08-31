export type MarketplaceSettingsProvider = 'avito' | 'ozon';

export interface ResolvedSettingsHash {
  tab: string;
  categoryProvider?: MarketplaceSettingsProvider;
  pricingProvider?: MarketplaceSettingsProvider;
}

const LEGACY_HASHES: Readonly<Record<string, ResolvedSettingsHash>> = {
  accounts: { tab: 'marketplaces' },
  'ozon-categories': {
    tab: 'marketplace-categories',
    categoryProvider: 'ozon',
  },
  pricing: {
    tab: 'marketplace-pricing',
    pricingProvider: 'avito',
  },
  'ozon-pricing': {
    tab: 'marketplace-pricing',
    pricingProvider: 'ozon',
  },
};

export function resolveSettingsHash(rawHash: string): ResolvedSettingsHash {
  const normalized = rawHash.trim().toLocaleLowerCase('ru-RU');
  const legacy = LEGACY_HASHES[normalized];
  if (legacy) return legacy;

  const marketplaceSection = normalized.match(
    /^marketplace-(categories|pricing)-(avito|ozon)$/,
  );
  if (marketplaceSection) {
    const provider = marketplaceSection[2] as MarketplaceSettingsProvider;
    return marketplaceSection[1] === 'categories'
      ? { tab: 'marketplace-categories', categoryProvider: provider }
      : { tab: 'marketplace-pricing', pricingProvider: provider };
  }
  return { tab: normalized };
}

export function settingsHash(
  tab: string,
  categoryProvider: MarketplaceSettingsProvider,
  pricingProvider: MarketplaceSettingsProvider,
): string {
  if (tab === 'marketplace-categories') {
    return `marketplace-categories-${categoryProvider}`;
  }
  if (tab === 'marketplace-pricing') {
    return `marketplace-pricing-${pricingProvider}`;
  }
  return tab;
}
