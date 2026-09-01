'use client';

import { AvitoCategorySettings } from '@/components/marketplaces/AvitoCategorySettings';
import { AvitoMarginSettings } from '@/components/marketplaces/AvitoMarginSettings';
import { MarketplaceSettingsSwitcher } from '@/components/marketplaces/MarketplaceSettingsSwitcher';
import { OzonPolicySettings } from '@/components/marketplaces/OzonPolicySettings';
import type { CatalogCategoryOption } from '@/components/products/catalog-category-picker';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { TabsContent } from '@/components/ui/tabs';
import type { MarketplaceAccount } from '@/lib/marketplace-account-types';
import {
  AVITO_PRICING_LABEL,
  MARKETPLACE_CATEGORIES_LABEL,
  MARKETPLACE_PRICING_LABEL,
} from '@/lib/marketplace-category-boundaries';
import type { MarketplaceSettingsProvider } from '@/lib/settings-marketplace-navigation';

interface Props {
  categoryProvider: MarketplaceSettingsProvider;
  pricingProvider: MarketplaceSettingsProvider;
  categories: CatalogCategoryOption[];
  ozonAccounts: MarketplaceAccount[];
  loadingCategories: boolean;
  loadingAccounts: boolean;
  canManage: boolean;
  ozonConnectionEnabled: boolean;
  savingCategoryId: number | null;
  onCategoryProviderChange: (provider: MarketplaceSettingsProvider) => void;
  onPricingProviderChange: (provider: MarketplaceSettingsProvider) => void;
  onToggleAvitoCategory: (categoryId: number) => void;
  onReloadCategories: () => Promise<void>;
}

export function MarketplaceCatalogSettingsSections({
  categoryProvider,
  pricingProvider,
  categories,
  ozonAccounts,
  loadingCategories,
  loadingAccounts,
  canManage,
  ozonConnectionEnabled,
  savingCategoryId,
  onCategoryProviderChange,
  onPricingProviderChange,
  onToggleAvitoCategory,
  onReloadCategories,
}: Props) {
  return (
    <>
      <TabsContent value="marketplace-categories" className="mt-4 space-y-4">
        <MarketplaceSettingsSwitcher
          value={categoryProvider}
          onChange={onCategoryProviderChange}
          title={MARKETPLACE_CATEGORIES_LABEL}
        />
        {categoryProvider === 'avito' ? (
          <AvitoCategorySettings
            categories={categories}
            loading={loadingCategories}
            canManage={canManage}
            savingCategoryId={savingCategoryId}
            onToggle={(category) => onToggleAvitoCategory(category.id)}
          />
        ) : (
          <OzonPolicySettings
            accounts={ozonAccounts}
            loading={loadingAccounts}
            canManage={canManage}
            connectionEnabled={ozonConnectionEnabled}
            mode="categories"
          />
        )}
      </TabsContent>

      <TabsContent value="marketplace-pricing" className="mt-4 space-y-4">
        <MarketplaceSettingsSwitcher
          value={pricingProvider}
          onChange={onPricingProviderChange}
          title={MARKETPLACE_PRICING_LABEL}
        />
        {pricingProvider === 'avito' ? (
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center gap-2">
                <CardTitle>{AVITO_PRICING_LABEL}</CardTitle>
                <Badge variant="outline">Общие для организации</Badge>
              </div>
              <CardDescription>
                Текущая проверенная схема рассчитывает цены листингов Avito по категориям
                каталога товаров. Наценку можно скорректировать в конкретном листинге.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
                Эти проценты относятся только к Avito. Переключитесь на Ozon, чтобы
                увидеть независимые правила выбранного кабинета Ozon.
              </div>
              <AvitoMarginSettings
                categories={categories.filter((category) => category.is_active)}
                onSaved={onReloadCategories}
              />
            </CardContent>
          </Card>
        ) : (
          <OzonPolicySettings
            accounts={ozonAccounts}
            loading={loadingAccounts}
            canManage={canManage}
            connectionEnabled={ozonConnectionEnabled}
            mode="margins"
          />
        )}
      </TabsContent>
    </>
  );
}
