export type ProductPhysicalFieldKey =
  | 'barcode'
  | 'length_mm'
  | 'width_mm'
  | 'height_mm'
  | 'weight_g'
  | 'vat_rate';

export type ProductPhysicalEffectiveSource = '1c' | 'map' | 'missing';

export interface ProductPhysicalMapProvenance {
  suggestion_id: number;
  source_id: string;
  source_label: string;
  source_url: string;
  raw_name: string;
  raw_value: string;
  accepted_at: string;
}

export interface ProductPhysicalSuggestion {
  id: number;
  field: Exclude<ProductPhysicalFieldKey, 'vat_rate'>;
  value: string;
  source_id: string;
  source_label: string;
  source_url: string;
  raw_name: string;
  raw_value: string;
  confidence: number;
  review_status: 'pending' | 'approved' | 'rejected';
  last_seen_at: string | null;
}

export interface ProductPhysicalFact {
  source_value: string | null;
  map_value: string | null;
  effective_value: string | null;
  effective_source: ProductPhysicalEffectiveSource;
  source_error: string;
  map_provenance: ProductPhysicalMapProvenance | null;
}

export interface ProductPhysicalProfile {
  facts: Record<ProductPhysicalFieldKey, ProductPhysicalFact>;
  suggestions: ProductPhysicalSuggestion[];
  units: {
    dimensions: 'mm';
    weight: 'g';
    vat: 'percent';
  };
  complete: boolean;
  missing_fields: ProductPhysicalFieldKey[];
  source_updated_at: string | null;
  updated_at: string | null;
}

export type ProductPhysicalDraft = Record<ProductPhysicalFieldKey, string>;

export const PRODUCT_PHYSICAL_FIELDS: Array<{
  key: ProductPhysicalFieldKey;
  label: string;
  unit: string;
  placeholder: string;
}> = [
  { key: 'barcode', label: 'Штрихкод', unit: '', placeholder: 'Например, 4601234567890' },
  { key: 'length_mm', label: 'Длина', unit: 'см', placeholder: '25' },
  { key: 'width_mm', label: 'Ширина', unit: 'см', placeholder: '12' },
  { key: 'height_mm', label: 'Высота', unit: 'см', placeholder: '8' },
  { key: 'weight_g', label: 'Вес', unit: 'кг', placeholder: '1,25' },
  { key: 'vat_rate', label: 'НДС', unit: '%', placeholder: '20' },
];

const EMPTY_DRAFT: ProductPhysicalDraft = {
  barcode: '',
  length_mm: '',
  width_mm: '',
  height_mm: '',
  weight_g: '',
  vat_rate: '',
};

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '';
  return new Intl.NumberFormat('ru-RU', {
    maximumFractionDigits: 3,
    useGrouping: false,
  }).format(value);
}

export function canonicalPhysicalValueToDisplay(
  field: ProductPhysicalFieldKey,
  value: string | null,
): string {
  if (!value) return '';
  if (field === 'barcode') return value;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '';
  if (field === 'weight_g') return formatNumber(numeric / 1000);
  if (field.endsWith('_mm')) return formatNumber(numeric / 10);
  return formatNumber(numeric);
}

export function physicalDraftFromProfile(
  profile: ProductPhysicalProfile | null | undefined,
): ProductPhysicalDraft {
  if (!profile) return { ...EMPTY_DRAFT };
  return Object.fromEntries(
    PRODUCT_PHYSICAL_FIELDS.map(({ key }) => [
      key,
      canonicalPhysicalValueToDisplay(key, profile.facts[key].map_value),
    ]),
  ) as ProductPhysicalDraft;
}

function requiredNumber(value: string, label: string): number | null {
  const normalized = value.trim().replace(',', '.');
  if (!normalized) return null;
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${label}: укажите положительное число.`);
  }
  return parsed;
}

function canonicalNumber(value: number, factor: number): string {
  return String(Math.round(value * factor * 1000) / 1000);
}

export function physicalDraftToApiPayload(
  draft: ProductPhysicalDraft,
): Record<ProductPhysicalFieldKey, string | null> {
  const length = requiredNumber(draft.length_mm, 'Длина');
  const width = requiredNumber(draft.width_mm, 'Ширина');
  const height = requiredNumber(draft.height_mm, 'Высота');
  const weight = requiredNumber(draft.weight_g, 'Вес');
  const vat = draft.vat_rate.trim();
  if (vat && !['0', '5', '7', '10', '20'].includes(vat)) {
    throw new Error('НДС: выберите 0%, 5%, 7%, 10% или 20%.');
  }
  return {
    barcode: draft.barcode.trim(),
    length_mm: length === null ? null : canonicalNumber(length, 10),
    width_mm: width === null ? null : canonicalNumber(width, 10),
    height_mm: height === null ? null : canonicalNumber(height, 10),
    weight_g: weight === null ? null : canonicalNumber(weight, 1000),
    vat_rate: vat || null,
  };
}

export function effectivePhysicalValueForInput(
  profile: ProductPhysicalProfile,
  field: ProductPhysicalFieldKey,
  draft: ProductPhysicalDraft,
): string {
  const fact = profile.facts[field];
  if (fact.effective_source === '1c') {
    return canonicalPhysicalValueToDisplay(field, fact.source_value);
  }
  return draft[field];
}
