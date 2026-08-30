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

  return (
    <div className="space-y-2">
      <div>
        <p className="text-sm font-medium">Маркетплейс и кабинет</p>
        <p className="text-xs text-muted-foreground">
          Переключайте карточку здесь. Данные Avito и Ozon сохраняются независимо.
        </p>
      </div>
      <div className="flex max-w-full gap-2 overflow-x-auto pb-1">
        {visibleAccounts.map((account) => {
          const selected = account.id === selectedAccountId;
          const state = states[account.id];
          return (
            <button
              key={`${account.marketplace}:${account.id}`}
              type="button"
              aria-pressed={selected}
              onClick={() => onSelect(account.id)}
              className={`min-w-[190px] rounded-lg border px-3 py-2 text-left transition-colors ${
                selected
                  ? 'border-primary bg-primary/5 ring-1 ring-primary/20'
                  : 'bg-background hover:bg-muted/50'
              }`}
            >
              <span className="block text-xs text-muted-foreground">
                {account.marketplace_label ?? (
                  account.marketplace === 'avito' ? 'Avito' : 'Ozon'
                )}
              </span>
              <span className="mt-0.5 block truncate text-sm font-medium">{account.name}</span>
              {state && (
                <Badge
                  className="mt-2 max-w-full whitespace-normal text-left text-[11px]"
                  variant={publicationTargetBadgeVariant(state.tone)}
                >
                  {state.label}
                </Badge>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
