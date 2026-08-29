export const AVITO_ACCOUNT_ACTIVE_LISTING_LIMIT = 10_000;

export interface ListingLimitPresentation {
  total: string;
  perAvitoAccount: string;
  requiresMultipleAvitoAccounts: boolean;
}

function formatInteger(value: number): string {
  return value.toLocaleString('ru-RU');
}

/**
 * The billing limit is tenant-wide, while one Avito Autoload feed has a
 * separate technical ceiling. Keeping both values in one presentation helper
 * prevents a plan card from promising that the whole allowance fits into one
 * Avito account.
 */
export function getListingLimitPresentation(
  tenantLimit: number | null,
): ListingLimitPresentation {
  const perAvitoAccountLimit = tenantLimit === null
    ? AVITO_ACCOUNT_ACTIVE_LISTING_LIMIT
    : Math.min(tenantLimit, AVITO_ACCOUNT_ACTIVE_LISTING_LIMIT);

  return {
    total: tenantLimit === null
      ? 'Без общего лимита активных объявлений в MAP'
      : `До ${formatInteger(tenantLimit)} активных объявлений всего в MAP`,
    perAvitoAccount: `До ${formatInteger(perAvitoAccountLimit)} в одном Avito-аккаунте`,
    requiresMultipleAvitoAccounts:
      tenantLimit === null || tenantLimit > AVITO_ACCOUNT_ACTIVE_LISTING_LIMIT,
  };
}
