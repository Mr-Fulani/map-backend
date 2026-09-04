export type DashboardNumber = number | string;

export type DashboardSeverity = 'critical' | 'warning' | 'info';
export type DashboardActivitySeverity = 'success' | 'error' | 'warning' | 'info';

export interface DashboardSubscription {
  plan: string | null;
  status: string | null;
  access_mode: 'full' | 'billing_only' | null;
  current_period_end: string | null;
  current_period_days_left: number | null;
  grace_days_left: number | null;
}

export interface DashboardLimitUsage {
  used: DashboardNumber;
  limit: DashboardNumber | null;
}

export interface DashboardAIUsage extends DashboardLimitUsage {
  successful_requests: number;
  included_balance: DashboardNumber;
  included_percent_used: DashboardNumber;
  purchased_balance: DashboardNumber;
  reserved_balance: DashboardNumber;
  total_balance: DashboardNumber;
  available_balance: DashboardNumber;
  included_expires_at: string | null;
  unlimited: boolean;
  individual_limit: boolean;
  overage_active: boolean;
  threshold: 'normal' | 'warning' | 'critical' | 'exhausted';
}

export interface DashboardUsage {
  listings: DashboardLimitUsage;
  sku: DashboardLimitUsage;
  ai_credits: DashboardAIUsage;
}

export interface DashboardAttentionItem {
  code: string;
  severity: DashboardSeverity;
  title: string;
  message: string;
  count: number | null;
  href: string | null;
  metadata: Record<string, unknown>;
}

export interface DashboardAnalyticsPoint {
  date: string;
  views: number;
  contacts: number;
  impressions: number;
}

export interface DashboardAnalytics {
  period_days: number;
  date_from: string;
  date_to: string;
  summary: {
    views: number;
    contacts: number;
    impressions: number;
    avg_ctr: number;
    active_listings: number;
  };
  daily: DashboardAnalyticsPoint[];
}

export interface DashboardFunnel {
  products: number;
  listings: number;
  active_listings: number;
  queued_listings: number;
  pending_listings: number;
  rejected_listings: number;
  requires_review_listings: number;
  limit_reached_listings: number;
}

export interface DashboardActivityItem {
  code: string;
  severity: DashboardActivitySeverity;
  title: string;
  message: string;
  occurred_at: string;
  product_id: number | null;
  listing_id: number | null;
  href: string | null;
  metadata: Record<string, unknown>;
}

export interface DashboardDatasourceItem {
  id: number;
  name: string;
  type: string;
  is_active: boolean;
  last_sync_at: string | null;
  last_sync_status: string;
  last_error: string;
}

export interface DashboardDatasources {
  total: number;
  active: number;
  healthy: number;
  errors: number;
  never_synced: number;
  latest_sync_at: string | null;
  returned_count: number;
  truncated: boolean;
  items: DashboardDatasourceItem[];
  latest_issues: Array<{
    id: number;
    name: string;
    last_sync_at: string | null;
    message: string;
  }>;
}

export interface DashboardAvitoAccount {
  account_id: number;
  account_name: string;
  is_active: boolean;
  connection_status: string;
  autoload_status: string;
  feed_configured: boolean | null;
  profile_stale: boolean;
  tariff_status: string;
  tariff_stale: boolean;
  subscription_ends_at: string | null;
  subscription_source: 'avito_tariff' | 'manual' | 'unavailable';
  days_left: number | null;
  placements_remaining: number | null;
  placements_total: number | null;
  last_error_code: string;
  last_error_message: string;
}

export interface DashboardServiceStatus {
  available: boolean;
  status: 'coming_soon' | 'available' | 'unavailable';
  used: DashboardNumber | null;
  limit: DashboardNumber | null;
  unit: string;
  title: string;
  description: string;
  uses_shared_ai_balance: boolean;
  href: string | null;
  metadata: Record<string, unknown>;
}

export interface DashboardSummary {
  generated_at: string;
  subscription: DashboardSubscription;
  usage: DashboardUsage;
  attention: DashboardAttentionItem[];
  analytics: DashboardAnalytics;
  funnel: DashboardFunnel;
  activity: DashboardActivityItem[];
  datasources: DashboardDatasources;
  marketplaces: {
    avito_total: number;
    ozon_total: number;
    avito_truncated: boolean;
    avito: DashboardAvitoAccount[];
  };
  services: {
    image_processing: DashboardServiceStatus;
  };
}

export type DashboardAIBalanceState =
  | 'unlimited'
  | 'exhausted'
  | 'purchased'
  | 'included_low'
  | 'normal';

export function dashboardNumber(value: DashboardNumber | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function dashboardPercent(
  used: DashboardNumber | null | undefined,
  limit: DashboardNumber | null | undefined,
): number | null {
  const normalizedUsed = dashboardNumber(used);
  const normalizedLimit = dashboardNumber(limit);
  if (normalizedUsed === null || normalizedLimit === null || normalizedLimit <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((normalizedUsed / normalizedLimit) * 100)));
}

export function hiddenTasksLabel(count: number): string {
  const lastTwoDigits = count % 100;
  const lastDigit = count % 10;
  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) {
    return `Ещё ${count} задач не показано`;
  }
  if (lastDigit === 1) return `Ещё ${count} задача не показана`;
  if (lastDigit >= 2 && lastDigit <= 4) return `Ещё ${count} задачи не показаны`;
  return `Ещё ${count} задач не показано`;
}

export function dashboardAIBalanceState(
  usage: Pick<DashboardAIUsage, 'available_balance' | 'overage_active' | 'threshold' | 'unlimited'>,
): DashboardAIBalanceState {
  if (usage.unlimited) return 'unlimited';
  const available = dashboardNumber(usage.available_balance);
  if (available !== null && available <= 0) return 'exhausted';
  if (usage.overage_active && available !== null && available > 0) return 'purchased';
  if (usage.threshold === 'warning' || usage.threshold === 'critical' || usage.threshold === 'exhausted') {
    return 'included_low';
  }
  return 'normal';
}

export function safeDashboardHref(value: string | null | undefined): string | null {
  if (!value || !value.startsWith('/') || value.startsWith('//')) return null;
  try {
    const parsed = new URL(value, 'https://dashboard.invalid');
    if (parsed.origin !== 'https://dashboard.invalid') return null;
    if (parsed.pathname !== '/dashboard' && !parsed.pathname.startsWith('/dashboard/')) return null;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}
