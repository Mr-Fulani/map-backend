import type {
  OzonCatalogTreeLevel,
  OzonCatalogTreeOption,
  OzonCategoryPolicySource,
} from './marketplace-account-types';

export type OzonEnabledDraft = 'inherit' | 'enabled' | 'disabled';

export interface OzonCategoryPolicyDraft {
  enabled: OzonEnabledDraft;
  margin: string;
}

export interface NormalizedOzonMargin {
  value: string | null;
  error: string | null;
}

export function ozonPolicyKey(option: OzonCatalogTreeOption): string {
  return `${option.description_category_id}:${option.type_id ?? 'category'}`;
}

export function ozonPolicyDraft(
  option: OzonCatalogTreeOption,
): OzonCategoryPolicyDraft {
  const override = option.policy.enabled_override;
  return {
    enabled: override === null ? 'inherit' : override ? 'enabled' : 'disabled',
    margin: option.policy.margin_pct ?? '',
  };
}

export function ozonEnabledOverride(
  draft: OzonEnabledDraft,
): boolean | null {
  if (draft === 'enabled') return true;
  if (draft === 'disabled') return false;
  return null;
}

export function normalizeOzonMargin(input: string): NormalizedOzonMargin {
  const normalized = input.trim().replace(',', '.');
  if (!normalized) return { value: null, error: null };
  if (!/^-?\d+(?:\.\d{1,2})?$/.test(normalized)) {
    return {
      value: null,
      error: 'Введите процент числом, максимум с двумя знаками после запятой.',
    };
  }
  const numeric = Number(normalized);
  if (!Number.isFinite(numeric) || numeric < -100 || numeric > 999.99) {
    return {
      value: null,
      error: 'Допустимое значение — от −100% до 999,99%.',
    };
  }
  return { value: normalized, error: null };
}

export function ozonCategoryPathIds(
  level: OzonCatalogTreeLevel,
  option: OzonCatalogTreeOption,
): number[] {
  const parentIds = level.path.map((item) => item.description_category_id);
  if (
    option.kind === 'type'
    && parentIds[parentIds.length - 1] === option.description_category_id
  ) {
    return parentIds;
  }
  return [...parentIds, option.description_category_id];
}

export function ozonPolicySourceLabel(
  source: OzonCategoryPolicySource | null,
  ownOption: OzonCatalogTreeOption,
  fallback: string,
): string {
  if (!source) return fallback;
  if (
    source.description_category_id === ownOption.description_category_id
    && source.type_id === ownOption.type_id
  ) {
    return 'задано здесь';
  }
  return `наследуется от «${source.name}»`;
}

export function ozonTreeLevelResponse(body: unknown): OzonCatalogTreeLevel {
  const candidate = (
    body
    && typeof body === 'object'
    && 'data' in body
  ) ? (body as { data: unknown }).data : body;

  if (
    !candidate
    || typeof candidate !== 'object'
    || !Array.isArray((candidate as { path?: unknown }).path)
    || !Array.isArray((candidate as { options?: unknown }).options)
  ) {
    throw new Error('Invalid local Ozon category tree response');
  }
  return candidate as OzonCatalogTreeLevel;
}
