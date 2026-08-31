'use client';

import { useMemo, useState } from 'react';
import { FolderTree, Percent, Store } from 'lucide-react';

import { OzonCatalogStatus } from '@/components/marketplaces/OzonCatalogStatus';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { MarketplaceAccount } from '@/lib/marketplace-account-types';
import {
  OZON_CATEGORIES_LABEL,
  OZON_PRICING_LABEL,
} from '@/lib/marketplace-category-boundaries';

interface OzonPolicySettingsProps {
  accounts: MarketplaceAccount[];
  loading: boolean;
  canManage: boolean;
  connectionEnabled: boolean;
  mode: 'categories' | 'margins';
}

export function OzonPolicySettings({
  accounts,
  loading,
  canManage,
  connectionEnabled,
  mode,
}: OzonPolicySettingsProps) {
  const preferredAccountId = useMemo(
    () => accounts.find((account) => account.is_active)?.id ?? accounts[0]?.id ?? null,
    [accounts],
  );
  const [accountChoiceId, setAccountChoiceId] = useState<number | null>(null);
  const selectedAccountId = accounts.some((account) => account.id === accountChoiceId)
    ? accountChoiceId
    : preferredAccountId;
  const selectedAccount = accounts.find((account) => account.id === selectedAccountId) ?? null;
  const title = mode === 'categories' ? OZON_CATEGORIES_LABEL : OZON_PRICING_LABEL;
  const Icon = mode === 'categories' ? FolderTree : Percent;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>{title}</CardTitle>
          <Badge variant="outline">Отдельно от Avito</Badge>
        </div>
        <CardDescription>
          {mode === 'categories'
            ? 'Официальное дерево Ozon для выбранного кабинета. Включайте и выключайте ветки так же понятно, как категории каталога.'
            : 'Наценки рассчитываются только для выбранного кабинета Ozon и наследуются по его собственному дереву.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <div className="space-y-2">
            <div className="h-16 animate-pulse rounded-lg bg-muted" />
            <div className="h-40 animate-pulse rounded-lg bg-muted" />
          </div>
        ) : accounts.length === 0 ? (
          <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            <Store className="mx-auto mb-2 h-5 w-5" />
            Сначала подключите кабинет Ozon во вкладке «Маркетплейсы».
          </div>
        ) : (
          <>
            <div className="rounded-lg border p-3">
              <p className="text-sm font-medium">Кабинет Ozon</p>
              <p className="mt-1 text-xs text-muted-foreground">
                У каждого кабинета своё дерево, доступность категорий и наценки.
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                {accounts.map((account) => (
                  <Button
                    key={account.id}
                    type="button"
                    size="sm"
                    variant={account.id === selectedAccount?.id ? 'default' : 'outline'}
                    onClick={() => setAccountChoiceId(account.id)}
                  >
                    {account.name}
                    {!account.is_active && (
                      <span className="ml-2 text-xs opacity-70">Выключен</span>
                    )}
                  </Button>
                ))}
              </div>
            </div>

            <div className="flex items-start gap-2 rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
              <Icon className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
              <p>
                {mode === 'categories'
                  ? 'Отключённая ветка не будет доступна при подготовке карточки Ozon. Товары и настройки не удаляются.'
                  : 'Пустая наценка наследуется от родителя. Если выше правило не задано, используется 0%.'}
              </p>
            </div>

            {selectedAccount && (
              <OzonCatalogStatus
                key={`${selectedAccount.id}:${mode}`}
                accountId={selectedAccount.id}
                accountName={selectedAccount.name}
                accountActive={selectedAccount.is_active}
                canManage={canManage}
                connectionEnabled={connectionEnabled}
                view={mode}
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
