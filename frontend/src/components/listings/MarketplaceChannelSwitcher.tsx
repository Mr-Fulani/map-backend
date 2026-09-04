'use client';

import { Badge } from '@/components/ui/badge';
import type { PublicationTargetState } from '@/lib/publication-workspace';
import { publicationTargetBadgeVariant } from '@/lib/publication-workspace';

export interface MarketplaceChannelAccount {
  id: number;
  name: string;
  marketplace: string;
  marketplace_label?: string;
  is_active: boolean;
}

export function MarketplaceChannelSwitcher({
  accounts,
  selectedAccountId,
  states = {},
  onSelect,
}: {
  accounts: MarketplaceChannelAccount[];
  selectedAccountId: number | null;
  states?: Record<number, PublicationTargetState | undefined>;
  onSelect: (accountId: number) => void;
}) {
  const visibleAccounts = accounts.filter((account) => (
    account.is_active && ['avito', 'ozon'].includes(account.marketplace)
  ));

  if (visibleAccounts.length === 0) return null;

  const marketplaceGroups = visibleAccounts.reduce<Record<string, MarketplaceChannelAccount[]>>(
    (groups, account) => {
      const group = groups[account.marketplace] ?? [];
      group.push(account);
      groups[account.marketplace] = group;
      return groups;
    },
    {},
  );
  const selectedAccount = visibleAccounts.find((account) => account.id === selectedAccountId)
    ?? visibleAccounts[0];
  const selectedMarketplace = selectedAccount.marketplace;
  const selectedMarketplaceAccounts = marketplaceGroups[selectedMarketplace] ?? [];
  const selectedState = states[selectedAccount.id];

  function providerLabel(account: MarketplaceChannelAccount) {
    return account.marketplace_label ?? (
      account.marketplace === 'avito' ? 'Avito' : 'Ozon'
    );
  }

  return (
    <div className="space-y-2">
      <div>
        <p className="text-sm font-medium">Маркетплейс и кабинет</p>
        <p className="text-xs text-muted-foreground">
          Переключайте карточку здесь. Данные Avito и Ozon сохраняются независимо.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(220px,0.8fr)] sm:items-end">
        <div className="min-w-0">
          <p className="mb-1 text-xs text-muted-foreground">Площадка</p>
          <div className="flex max-w-full gap-1 overflow-x-auto rounded-lg bg-muted p-1">
            {Object.values(marketplaceGroups).map((group) => {
              const account = group[0];
              const selected = account.marketplace === selectedMarketplace;
              return (
                <button
                  key={account.marketplace}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => onSelect(account.id)}
                  className={`flex min-w-[110px] flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                    selected ? 'bg-background shadow-sm' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {providerLabel(account)}
                  {group.length > 1 && (
                    <span className="text-xs text-muted-foreground">· {group.length}</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
        <div className="min-w-0">
          <label htmlFor="publication-workspace-account" className="mb-1 block text-xs text-muted-foreground">
            Кабинет
          </label>
          <select
            id="publication-workspace-account"
            value={selectedAccount.id}
            onChange={(event) => onSelect(Number(event.target.value))}
            className="h-10 w-full min-w-0 rounded-md border bg-background px-3 text-sm font-medium"
          >
            {selectedMarketplaceAccounts.map((account) => (
              <option key={account.id} value={account.id}>{account.name}</option>
            ))}
          </select>
        </div>
      </div>
      {selectedState && (
        <div className="flex items-center justify-between gap-3 rounded-md border bg-muted/20 px-3 py-2">
          <span className="min-w-0 truncate text-xs text-muted-foreground">
            {providerLabel(selectedAccount)} · {selectedAccount.name}
          </span>
          <Badge
            className="max-w-[55%] shrink-0 whitespace-normal text-right text-[11px]"
            variant={publicationTargetBadgeVariant(selectedState.tone)}
          >
            {selectedState.label}
          </Badge>
        </div>
      )}
    </div>
  );
}
