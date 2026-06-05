export const DASHBOARD_CURRENT_HREF_KEY = 'dashboard.navigation.currentHref';
export const DASHBOARD_PREVIOUS_HREF_KEY = 'dashboard.navigation.previousHref';

export function isSafeDashboardHref(value: string | null) {
  return Boolean(value && value.startsWith('/dashboard') && !value.startsWith('//'));
}

export function getPreviousDashboardHref() {
  if (typeof window === 'undefined') return null;

  const href = window.sessionStorage.getItem(DASHBOARD_PREVIOUS_HREF_KEY);
  return isSafeDashboardHref(href) ? href : null;
}
