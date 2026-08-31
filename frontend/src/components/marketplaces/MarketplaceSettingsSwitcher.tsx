'use client';

import { Store } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { MarketplaceSettingsProvider } from '@/lib/settings-marketplace-navigation';

const PROVIDERS: ReadonlyArray<{
  id: MarketplaceSettingsProvider;
  name: string;
  description: string;
}> = [{
  id: 'avito',
  name: 'Avito',
  description: 'Общие настройки организации',
}, {
  id: 'ozon',
  name: 'Ozon',
  description: 'Настройки выбранного кабинета',
}];

export function MarketplaceSettingsSwitcher({
  value,
  onChange,
  title,
}: {
  value: MarketplaceSettingsProvider;
  onChange: (provider: MarketplaceSettingsProvider) => void;
  title: string;
}) {
  return (
    <div className="space-y-3 rounded-xl border bg-muted/20 p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="flex items-center gap-2 text-sm font-medium">
            <Store className="h-4 w-4" /> {title}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            Выберите площадку. Структура экрана одинаковая, данные и правила не смешиваются.
          </p>
        </div>
        <Badge variant="outline">Один принцип работы</Badge>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {PROVIDERS.map((provider) => (
          <Button
            key={provider.id}
            type="button"
            variant={value === provider.id ? 'default' : 'outline'}
            className="h-auto min-h-14 justify-start px-3 py-2 text-left"
            onClick={() => onChange(provider.id)}
          >
            <span>
              <span className="block font-medium">{provider.name}</span>
              <span className={`block text-xs ${
                value === provider.id ? 'text-primary-foreground/80' : 'text-muted-foreground'
              }`}
              >
                {provider.description}
              </span>
            </span>
          </Button>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Яндекс Маркет, Wildberries и другие площадки будут добавляться сюда, без новых разрозненных разделов.
      </p>
    </div>
  );
}
