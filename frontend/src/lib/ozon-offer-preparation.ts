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
    updated_at: string;
  };
  preflight: {
    ready: boolean;
    errors: OzonPreflightIssue[];
    recommendations: OzonPreflightIssue[];
  };
}
