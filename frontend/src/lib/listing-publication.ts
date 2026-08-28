export const PUBLICATION_FIELD_ORDER = [
  'product_brand',
  'catalog_category',
  'title',
  'description_ai',
  'account_id',
  'price_on_listing',
  'manager_name_override',
  'contact_phone_override',
] as const;

export type PublicationField = typeof PUBLICATION_FIELD_ORDER[number];
export type PublicationFieldErrors = Partial<Record<PublicationField, string[]>>;

export function firstPublicationErrorField(
  errors: PublicationFieldErrors,
): PublicationField | null {
  return PUBLICATION_FIELD_ORDER.find((field) => (errors[field]?.length ?? 0) > 0) ?? null;
}

export function hasPublicationFieldErrors(errors: PublicationFieldErrors): boolean {
  return firstPublicationErrorField(errors) !== null;
}

export function publicationFieldErrorsFromApi(payload: unknown): PublicationFieldErrors {
  if (!payload || typeof payload !== 'object') return {};
  const response = payload as Record<string, unknown>;
  const source = response.field_errors && typeof response.field_errors === 'object'
    ? response.field_errors as Record<string, unknown>
    : response;
  const errors: PublicationFieldErrors = {};
  for (const field of PUBLICATION_FIELD_ORDER) {
    const raw = source[field];
    if (typeof raw === 'string' && raw.trim()) errors[field] = [raw];
    if (Array.isArray(raw)) {
      const messages = raw.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
      if (messages.length > 0) errors[field] = messages;
    }
  }
  return errors;
}

export function publicationActionLabel(deliveryStage: string): string {
  return deliveryStage === 'delivery_failed'
    ? 'Исправить и отправить снова'
    : 'Опубликовать';
}
