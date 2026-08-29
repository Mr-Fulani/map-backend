'use client';

interface MarketplaceAccountOption {
  id: number;
  name: string;
  marketplace: string;
}

interface Props {
  marketplace: string;
  accountId: number | null;
  accounts: MarketplaceAccountOption[];
  onChange: (next: { marketplace: string; accountId: number | null }) => void;
  className?: string;
}

export function marketplaceDisplayName(value: string): string {
  if (value === 'avito') return 'Avito';
  if (value === 'ozon') return 'Ozon';
  return value || 'Все маркетплейсы';
}

export default function MarketplaceAccountFilter({
  marketplace,
  accountId,
  accounts,
  onChange,
  className = '',
}: Props) {
  const visibleAccounts = marketplace
    ? accounts.filter((account) => account.marketplace === marketplace)
    : accounts;
  const selectedAccount = visibleAccounts.some((account) => account.id === accountId)
    ? accountId
    : null;

  return (
    <div className={`grid gap-2 sm:grid-cols-2 ${className}`.trim()}>
      <label className="space-y-1 text-xs text-muted-foreground">
        <span>Маркетплейс</span>
        <select
          value={marketplace}
          onChange={(event) => onChange({
            marketplace: event.target.value,
            accountId: null,
          })}
          className="h-9 w-full rounded-md border bg-background px-3 text-sm text-foreground"
        >
          <option value="">Все маркетплейсы</option>
          <option value="avito">Avito</option>
          <option value="ozon" disabled>Ozon — подключение следующим этапом</option>
        </select>
      </label>
      <label className="space-y-1 text-xs text-muted-foreground">
        <span>Аккаунт</span>
        <select
          value={selectedAccount ?? ''}
          onChange={(event) => onChange({
            marketplace,
            accountId: event.target.value ? Number(event.target.value) : null,
          })}
          className="h-9 w-full rounded-md border bg-background px-3 text-sm text-foreground"
        >
          <option value="">Все аккаунты</option>
          {visibleAccounts.map((account) => (
            <option key={account.id} value={account.id}>
              {marketplace ? account.name : `${marketplaceDisplayName(account.marketplace)} · ${account.name}`}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
