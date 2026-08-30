export interface AutoloadOnboarding {
  state: 'legacy' | 'pending' | 'retrying' | 'reconciling' | 'ready' | 'exhausted' | 'manual_review';
  profile_state: string;
  ready: boolean;
  retryable: boolean;
  message: string;
}

export interface AvitoAccountHealth {
  connection_status: 'unknown' | 'connected' | 'auth_error' | 'unavailable';
  autoload_status: 'unknown' | 'enabled' | 'disabled' | 'missing' | 'forbidden';
  feed_configured: boolean | null;
  profile_checked_at: string | null;
  profile_stale: boolean;
  tariff_status: 'unknown' | 'active' | 'inactive' | 'not_found' | 'unavailable';
  tariff_name: string;
  tariff_started_at: string | null;
  tariff_ends_at: string | null;
  subscription_ends_at: string | null;
  subscription_source: 'avito_tariff' | 'manual' | 'unavailable';
  days_left: number | null;
  tariff_price: string | null;
  placements_remaining: number | null;
  placements_total: number | null;
  scheduled_tariff: { name?: string; starts_at?: string | null; price?: string | null };
  tariff_checked_at: string | null;
  tariff_stale: boolean;
  last_attempted_at: string | null;
  last_error_code: string;
  last_error_message: string;
}

export interface OzonAccountProfile {
  connection_status: 'connected' | 'warehouse_missing' | 'warehouse_selection_required';
  company_name: string;
  seller_name: string;
  currency: string;
  roles: string[];
  api_methods: string[];
  api_key_expires_at: string | null;
  warehouse_count: number;
  selected_warehouse_id: string;
  selected_warehouse_name: string;
  last_checked_at: string;
}

export interface OzonCatalogTreeMetadata {
  revision: string;
  language: string;
  node_count: number;
  active_type_count: number;
  first_synced_at: string;
  last_checked_at: string;
}

export interface OzonCatalogAttributeMetadata {
  revision: string;
  description_category_id: number;
  type_id: number;
  language: string;
  attribute_count: number;
  required_attribute_count: number;
  first_synced_at: string;
  last_checked_at: string;
}

export interface OzonCatalogState {
  account_id: number;
  marketplace: 'ozon';
  tree: OzonCatalogTreeMetadata | null;
  attribute_schema_count: number;
  latest_attribute_schema: OzonCatalogAttributeMetadata | null;
}

export interface OzonCatalogTypeItem {
  description_category_id: number;
  type_id: number;
  category_path: string;
  type_name: string;
}

export interface OzonCatalogTypesPage {
  status: 'ok';
  data: OzonCatalogTypeItem[];
  meta: {
    total: number;
    page: number;
    page_size: number;
    next: string | null;
    prev: string | null;
    tree_revision: string | null;
    tree_checked_at: string | null;
    language: string;
  };
}

export interface MarketplaceProviderCapabilities {
  account_health: boolean;
  catalog_schema: boolean;
  publication_preflight: boolean;
  publish_or_update: boolean;
  price_update: boolean;
  stock_update: boolean;
  archive: boolean;
  status_reconcile: boolean;
  statistics: boolean;
}

export interface MarketplaceAccount {
  id: number;
  name: string;
  marketplace: string;
  marketplace_label: string;
  provider_capabilities: MarketplaceProviderCapabilities;
  external_id: string;
  is_active: boolean;
  default_address: string;
  default_seller_address_id: string;
  default_manager_name: string;
  default_contact_phone: string;
  autoload_active: boolean | null;
  autoload_checked_at: string | null;
  autoload_subscription_ends_at: string | null;
  feed_endpoint_managed: boolean;
  autoload_onboarding: AutoloadOnboarding | null;
  avito_status: AvitoAccountHealth | null;
  ozon_profile: OzonAccountProfile | null;
  created_at: string;
}

export interface MarketplaceProviderRollout {
  ozon: {
    account_connection_enabled: boolean;
    credential_update_enabled: boolean;
  };
}
