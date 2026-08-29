export const PUBLICATION_FIELD_ORDER = [
  'images',
  'title',
  'description_ai',
  'product_brand',
  'product_oem',
  'account_id',
  'price_on_listing',
  'catalog_category',
  'placement_address',
  'manager_name_override',
  'contact_phone_override',
] as const;

export type PublicationField = typeof PUBLICATION_FIELD_ORDER[number];
export type PublicationFieldIssues = Partial<Record<PublicationField, string[]>>;
export type PublicationFieldErrors = PublicationFieldIssues;
export type PublicationFieldWarnings = PublicationFieldIssues;

export function firstPublicationErrorField(
  errors: PublicationFieldErrors,
): PublicationField | null {
  return PUBLICATION_FIELD_ORDER.find((field) => (errors[field]?.length ?? 0) > 0) ?? null;
}

export function hasPublicationFieldErrors(errors: PublicationFieldErrors): boolean {
  return firstPublicationErrorField(errors) !== null;
}

export function firstPublicationWarningField(
  warnings: PublicationFieldWarnings,
): PublicationField | null {
  return PUBLICATION_FIELD_ORDER.find((field) => (warnings[field]?.length ?? 0) > 0) ?? null;
}

export function hasPublicationFieldWarnings(warnings: PublicationFieldWarnings): boolean {
  return firstPublicationWarningField(warnings) !== null;
}

export function publicationFieldErrorsFromApi(payload: unknown): PublicationFieldErrors {
  if (!payload || typeof payload !== 'object') return {};
  const response = payload as Record<string, unknown>;
  const source = response.field_errors && typeof response.field_errors === 'object'
    ? response.field_errors as Record<string, unknown>
    : response;
  const errors: PublicationFieldErrors = {};
  for (const field of PUBLICATION_FIELD_ORDER) {
    const apiField = field === 'product_oem' ? 'avito_oem' : field;
    const raw = source[field] ?? source[apiField];
    if (typeof raw === 'string' && raw.trim()) errors[field] = [raw];
    if (Array.isArray(raw)) {
      const messages = raw.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
      if (messages.length > 0) errors[field] = messages;
    }
  }
  return errors;
}

export function publicationActionLabel(
  deliveryStage: string,
  rejectionReadyToRetry = false,
): string {
  if (rejectionReadyToRetry) return 'Отправить исправленную версию';
  return deliveryStage === 'delivery_failed'
    ? 'Исправить и отправить снова'
    : 'Опубликовать';
}
