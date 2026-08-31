import type { OzonCategoryPolicyState } from './marketplace-account-types';

export interface OzonOfferValue {
  value: string;
  dictionary_value_id: number;
}

export interface OzonOfferAttribute {
  id: number;
  complex_id: number;
  attribute_complex_id: number;
  name: string;
  description: string;
  type: string;
  is_required: boolean;
  max_value_count: number;
  dictionary_id: number;
  selected_values: OzonOfferValue[];
}

export interface OzonPreflightIssue {
  code: string;
  field: string;
  label: string;
  message: string;
}

export interface OzonOfferPreparation {
  account: { id: number; name: string; marketplace: 'ozon' };
  draft: null | {
    id: number;
    offer_id: string;
    category: null | {
      description_category_id: number;
      type_id: number;
      category_path: string;
      type_name: string;
      tree_revision: string;
    };
    attribute_schema_revision: string;
    updated_at: string;
  };
  attributes: OzonOfferAttribute[];
  schema: null | {
    revision: string;
    attribute_count: number;
    required_attribute_count: number;
    updated_at: string;
  };
  pricing: null | {
    base_price: string;
    effective_margin_pct: string;
    final_price: string;
    policy: OzonCategoryPolicyState;
  };
  preflight: {
    ready: boolean;
    errors: OzonPreflightIssue[];
    recommendations: OzonPreflightIssue[];
  };
}

export interface OzonDictionaryValue {
  id: number;
  value: string;
  info: string;
  picture: string;
}

export function ozonAttributesPayload(attributes: OzonOfferAttribute[]) {
  return attributes
    .filter((attribute) => attribute.selected_values.length > 0)
    .map((attribute) => ({
      id: attribute.id,
      complex_id: attribute.complex_id,
      values: attribute.selected_values.map((value) => ({
        value: value.value.trim(),
        dictionary_value_id: value.dictionary_value_id,
      })),
    }));
}

export function replaceOzonAttributeValue(
  attributes: OzonOfferAttribute[],
  attributeId: number,
  complexId: number,
  value: OzonOfferValue | null,
): OzonOfferAttribute[] {
  return attributes.map((attribute) => (
    attribute.id === attributeId && attribute.complex_id === complexId
      ? { ...attribute, selected_values: value ? [value] : [] }
      : attribute
  ));
}
