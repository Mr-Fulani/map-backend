'use client';

import { Loader2, RotateCcw, Save } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { OzonOfferPreparation } from '@/lib/ozon-offer-preparation';
import type { OzonPricingMode } from '@/lib/ozon-offer-pricing';

type Pricing = NonNullable<OzonOfferPreparation['pricing']>;

function rubles(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ₽`;
}

function percent(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}%`;
}

interface Props {
  accountId: number | null;
  pricing: Pricing;
  mode: OzonPricingMode;
  margin: string;
  price: string;
  saving: boolean;
  onMarginChange: (value: string) => void;
  onPriceChange: (value: string) => void;
  onSave: () => void;
}

export function OzonOfferPricingEditor({
  accountId,
  pricing,
  mode,
  margin,
  price,
  saving,
  onMarginChange,
  onPriceChange,
  onSave,
}: Props) {
  return (
    <div className={`space-y-3 rounded-md border p-3 ${
      pricing.policy.effective_enabled
        ? 'bg-muted/20'
        : 'border-amber-500/30 bg-amber-500/5'
    }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">Цена для Ozon</p>
          <p className="text-xs text-muted-foreground">
            Рассчитывается отдельно для выбранного кабинета. Цена товара и Avito не меняются.
          </p>
        </div>
        <Badge variant={pricing.policy.effective_enabled ? 'outline' : 'destructive'}>
          {pricing.policy.effective_enabled ? 'Категория включена' : 'Категория выключена'}
        </Badge>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <p className="text-xs text-muted-foreground">Цена товара</p>
          <p className="font-medium tabular-nums">{rubles(pricing.base_price)}</p>
        </div>
        <div className="space-y-1">
          <label htmlFor={`ozon-offer-margin-${accountId}`} className="text-xs text-muted-foreground">
            Наценка Ozon, %
          </label>
          <Input
            id={`ozon-offer-margin-${accountId}`}
            value={margin}
            inputMode="decimal"
            placeholder={pricing.policy.effective_margin_pct}
            disabled={saving}
            onChange={(event) => onMarginChange(event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={`ozon-offer-price-${accountId}`} className="text-xs text-muted-foreground">
            Итоговая цена Ozon, ₽
          </label>
          <Input
            id={`ozon-offer-price-${accountId}`}
            value={price}
            inputMode="decimal"
            disabled={saving}
            onChange={(event) => onPriceChange(event.target.value)}
          />
        </div>
      </div>

      <div className="flex flex-col gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-muted-foreground">
          {mode === 'price'
            ? 'Индивидуальная точная цена этого товара и кабинета. Avito не изменяется.'
            : mode === 'margin'
              ? 'Индивидуальная наценка этого товара и кабинета. Avito не изменяется.'
              : pricing.policy.margin_source
                ? `Наследуется правило «${pricing.policy.margin_source.name}»: ${percent(pricing.policy.effective_margin_pct)}.`
                : 'Наследуется стандартная наценка 0%.'}
        </p>
        <div className="flex shrink-0 gap-2">
          {mode !== 'inherited' && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={saving}
              onClick={() => onMarginChange('')}
            >
              <RotateCcw className="mr-1 h-3.5 w-3.5" /> Наследовать
            </Button>
          )}
          <Button type="button" size="sm" disabled={saving} onClick={onSave}>
            {saving
              ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              : <Save className="mr-1 h-3.5 w-3.5" />}
            Сохранить цену
          </Button>
        </div>
      </div>
    </div>
  );
}
