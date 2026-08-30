export type DashboardQueryValue = string | number | null | undefined;

export function dashboardQueryHref(
  pathname: string,
  currentSearch: string,
  updates: Record<string, DashboardQueryValue>,
): string {
  const next = new URLSearchParams(currentSearch);
  Object.entries(updates).forEach(([key, value]) => {
    if (value === null || value === undefined || value === '') next.delete(key);
    else next.set(key, String(value));
  });
  return next.size ? `${pathname}?${next.toString()}` : pathname;
}

export function dashboardPageParam(value: string | null): number {
  if (!value || !/^\d+$/.test(value)) return 1;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 1;
}

export function dashboardMarketplaceParam(
  value: string | null,
  supported: readonly string[] = ['avito', 'ozon'],
): string {
  return value && supported.includes(value) ? value : '';
}

export function dashboardPositiveIdParam(value: string | null): number | null {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}
