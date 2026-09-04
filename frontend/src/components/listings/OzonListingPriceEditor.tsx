'use client';

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useState,
} from 'react';
import { Loader2, RotateCcw, Save } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { productApi } from '@/lib/api';
import type { OzonOfferPreparation } from '@/lib/ozon-offer-preparation';
import {
  ozonMarginFromPrice,
  ozonPriceFromMargin,
  ozonPricingPayload,
  type OzonPricingMode,
} from '@/lib/ozon-offer-pricing';

export interface OzonListingPriceEditorHandle {
  applyMarketPrice: (price: string) => boolean;
  focus: () => void;
}

interface Props {
  productId: number;
  accountId: number;
  accountName: string;
  stockQty: number;
  preparation: OzonOfferPreparation | null;
  onPreparationChange: (preparation: OzonOfferPreparation) => void;
}

function envelopeData<T>(body: unknown): T {
  return (body as { data: T }).data;
}

function initialMode(preparation: OzonOfferPreparation | null): OzonPricingMode {
  if (preparation?.draft?.price_override !== null
    && preparation?.draft?.price_override !== undefined) return 'price';
  if (preparation?.draft?.margin_pct !== null
    && preparation?.draft?.margin_pct !== undefined) return 'margin';
  return 'inherited';
}

function rubles(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ₽`;
}

export const OzonListingPriceEditor = forwardRef<
OzonListingPriceEditorHandle,
Props
>(function OzonListingPriceEditor({
  productId,
  accountId,
  accountName,
  stockQty,
  preparation,
  onPreparationChange,
}, ref) {
  const pricing = preparation?.pricing ?? null;
  const [mode, setMode] = useState<OzonPricingMode>(() => initialMode(preparation));
  const [margin, setMargin] = useState(preparation?.pricing?.margin_override ?? '');
  const [price, setPrice] = useState(preparation?.pricing?.final_price ?? '');
  const [saving, setSaving] = useState(false);
  const priceInputId = `ozon-listing-price-${accountId}`;
  const validation = ozonPricingPayload(mode, margin, price);

  useEffect(() => {
    setMode(initialMode(preparation));
    setMargin(preparation?.pricing?.margin_override ?? '');
    setPrice(preparation?.pricing?.final_price ?? '');
  }, [preparation]);

  function updateMargin(nextMargin: string) {
    setMargin(nextMargin);
    if (!pricing) return;
    if (nextMargin.trim() === '') {
      setMode('inherited');
      setPrice(
        pricing.policy.effective_margin_pct
          ? (Number(pricing.base_price) * (
            1 + Number(pricing.policy.effective_margin_pct) / 100
          )).toFixed(2)
          : pricing.final_price,
      );
      return;
    }
    setMode('margin');
    const nextPrice = ozonPriceFromMargin(pricing.base_price, nextMargin);
    if (nextPrice !== null) setPrice(nextPrice);
  }

  function updatePrice(nextPrice: string) {
    setPrice(nextPrice);
    setMode('price');
    if (!pricing) return;
    const nextMargin = ozonMarginFromPrice(pricing.base_price, nextPrice);
    if (nextMargin !== null) setMargin(nextMargin);
  }

  function applyMarketPrice(nextPrice: string): boolean {
    if (!pricing) {
      toast.error('Дождитесь загрузки цены Ozon и повторите выбор.');
      return false;
    }
    const nextMargin = ozonMarginFromPrice(pricing.base_price, nextPrice);
    const numericPrice = Number(nextPrice);
    if (nextMargin === null || !Number.isFinite(numericPrice)) {
      toast.error('Не удалось рассчитать наценку по выбранной рыночной цене.');
      return false;
    }
    setPrice(numericPrice.toFixed(2));
    setMargin(nextMargin);
    setMode('price');
    document.getElementById(priceInputId)?.focus({ preventScroll: true });
    return true;
  }

  useImperativeHandle(ref, () => ({
    applyMarketPrice,
    focus: () => document.getElementById(priceInputId)?.focus({ preventScroll: true }),
  }));

  async function savePrice() {
    const payload = ozonPricingPayload(mode, margin, price);
    if (!payload.ok) {
      toast.error(payload.message);
      return;
    }
    setSaving(true);
    try {
      const response = await productApi.updateOzonOffer(productId, {
        account_id: accountId,
        ...payload.payload,
      });
      const next = envelopeData<OzonOfferPreparation>(response.data);
      onPreparationChange(next);
      toast.success('Цена Ozon сохранена для выбранного кабинета.');
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось сохранить цену Ozon.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      data-testid="ozon-price-account-section"
      data-ozon-section="pricing"
      className={`space-y-3 rounded-md border border-l-4 p-3 ${
        pricing?.policy.effective_enabled === false
          ? 'border-amber-500/50 bg-amber-500/5'
          : 'border-blue-500/35 bg-blue-500/5'
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">3. Кабинет, цена и остаток</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Настройки только для Ozon. Цена Avito и цена товара не изменятся.
          </p>
        </div>
        <Badge
          variant="outline"
          className={pricing?.policy.effective_enabled === false
            ? 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'
            : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200'}
        >
          {pricing?.policy.effective_enabled === false ? 'Категория выключена' : 'Цена рассчитана'}
        </Badge>
      </div>

      <p className="rounded-md border border-blue-500/25 bg-background p-2.5 text-xs text-muted-foreground">
        Если рассчитанная цена подходит, ничего менять не нужно. Индивидуальная наценка
        или точная цена сохраняются только для этого товара и кабинета Ozon.
      </p>

      <div className="grid gap-3 rounded-md border bg-background p-3 sm:grid-cols-2">
        <div className="space-y-1">
          <p className="text-sm text-muted-foreground">Кабинет Ozon</p>
          <p className="flex h-9 items-center text-sm font-medium">{accountName}</p>
        </div>
        <div className="space-y-1">
          <p className="text-sm text-muted-foreground">Базовая цена товара</p>
          <p className="flex h-9 items-center text-sm text-muted-foreground">
            {pricing ? rubles(pricing.base_price) : 'Загружается…'}
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={`ozon-listing-margin-${accountId}`} className="text-sm text-muted-foreground">
            Наценка Ozon, % <span className="text-xs">(необязательно)</span>
          </label>
          <Input
            id={`ozon-listing-margin-${accountId}`}
            value={margin}
            inputMode="decimal"
            placeholder={pricing?.policy.effective_margin_pct ?? '0'}
            disabled={!pricing || saving}
            onChange={(event) => updateMargin(event.target.value)}
            aria-invalid={mode === 'margin' && !validation.ok}
            className={mode === 'margin' && !validation.ok ? 'border-destructive' : undefined}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={priceInputId} className="text-sm text-muted-foreground">
            Цена объявления Ozon, ₽ <span className="text-xs">(необязательно)</span>
          </label>
          <Input
            id={priceInputId}
            value={price}
            inputMode="decimal"
            disabled={!pricing || saving}
            onChange={(event) => updatePrice(event.target.value)}
            aria-invalid={mode === 'price' && !validation.ok}
            className={mode === 'price' && !validation.ok ? 'border-destructive' : undefined}
          />
        </div>
      </div>

      {!validation.ok && (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2.5 text-xs text-destructive">
          {validation.message}
        </p>
      )}

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-xs text-muted-foreground">
          <p>Остаток для отправки: <strong className="text-foreground">{stockQty} шт.</strong></p>
          <p>
            {mode === 'price'
              ? 'Установлена индивидуальная цена для этого товара и кабинета.'
              : mode === 'margin'
                ? 'Установлена индивидуальная наценка для этого товара и кабинета.'
                : pricing?.policy.margin_source
                  ? `Наследуется правило «${pricing.policy.margin_source.name}».`
                  : 'Наследуется стандартная наценка.'}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          {mode !== 'inherited' && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={saving}
              onClick={() => updateMargin('')}
            >
              <RotateCcw className="mr-1.5 h-4 w-4" /> Наследовать
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            disabled={!pricing || saving || !validation.ok}
            onClick={() => void savePrice()}
          >
            {saving
              ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              : <Save className="mr-1.5 h-4 w-4" />}
            Сохранить цену
          </Button>
        </div>
      </div>
    </div>
  );
});
