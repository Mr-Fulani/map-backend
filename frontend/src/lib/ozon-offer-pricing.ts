export type OzonPricingMode = 'inherited' | 'margin' | 'price';

export interface OzonPricingPayload {
  margin_pct: string | null;
  price_override: string | null;
}

export type OzonPricingPayloadResult =
  | { ok: true; payload: OzonPricingPayload }
  | { ok: false; message: string };

function normalizedDecimal(value: string): string {
  return value.trim().replace(',', '.');
}

export function ozonPriceFromMargin(basePrice: string, margin: string): string | null {
  const base = Number(normalizedDecimal(basePrice));
  const percentage = Number(normalizedDecimal(margin));
  if (!Number.isFinite(base) || base <= 0 || !Number.isFinite(percentage)) return null;
  const price = base * (1 + percentage / 100);
  return price > 0 ? price.toFixed(2) : null;
}

export function ozonMarginFromPrice(basePrice: string, price: string): string | null {
  const base = Number(normalizedDecimal(basePrice));
  const target = Number(normalizedDecimal(price));
  if (!Number.isFinite(base) || base <= 0 || !Number.isFinite(target) || target <= 0) {
    return null;
  }
  return (((target / base) - 1) * 100).toFixed(2);
}

export function ozonPricingPayload(
  mode: OzonPricingMode,
  marginInput: string,
  priceInput: string,
): OzonPricingPayloadResult {
  if (mode === 'inherited') {
    return { ok: true, payload: { margin_pct: null, price_override: null } };
  }
  if (mode === 'margin') {
    const margin = normalizedDecimal(marginInput);
    if (!/^-?\d{1,5}(?:\.\d{1,2})?$/.test(margin) || Number(margin) <= -100) {
      return { ok: false, message: 'Наценка Ozon должна быть числом больше −100% с точностью до сотых.' };
    }
    return { ok: true, payload: { margin_pct: margin, price_override: null } };
  }

  const price = normalizedDecimal(priceInput);
  if (!/^\d{1,10}(?:\.\d{1,2})?$/.test(price) || Number(price) <= 0) {
    return { ok: false, message: 'Цена Ozon должна быть числом больше нуля с точностью до копеек.' };
  }
  return { ok: true, payload: { margin_pct: null, price_override: price } };
}
