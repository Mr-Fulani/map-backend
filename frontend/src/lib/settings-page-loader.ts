export type SettingsLoadGroup =
  | 'api-keys'
  | 'marketplaces'
  | 'datasources'
  | 'catalog-domains'
  | 'catalog'
  | 'pricing-categories'
  | 'ai'
  | 'notifications';

const TAB_LOAD_GROUPS: Readonly<Record<string, readonly SettingsLoadGroup[]>> = {
  'api-keys': ['api-keys'],
  marketplaces: ['marketplaces'],
  'ozon-categories': ['marketplaces'],
  'ozon-pricing': ['marketplaces'],
  datasources: ['datasources'],
  'catalog-categories': ['catalog-domains', 'catalog'],
  pricing: ['pricing-categories'],
  ai: ['ai'],
  notifications: ['notifications'],
};

export function settingsLoadGroups(tab: string): readonly SettingsLoadGroup[] {
  return TAB_LOAD_GROUPS[tab] ?? [];
}

export function claimSettingsLoadGroups(
  tab: string,
  claimed: Set<SettingsLoadGroup>,
): SettingsLoadGroup[] {
  const groups: SettingsLoadGroup[] = [];
  for (const group of settingsLoadGroups(tab)) {
    if (claimed.has(group)) continue;
    claimed.add(group);
    groups.push(group);
  }
  return groups;
}
