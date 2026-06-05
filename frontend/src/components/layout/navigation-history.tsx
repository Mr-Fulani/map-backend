'use client';

import { useEffect } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import {
  DASHBOARD_CURRENT_HREF_KEY,
  DASHBOARD_PREVIOUS_HREF_KEY,
} from '@/lib/navigation-history';

export function DashboardNavigationHistory() {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  useEffect(() => {
    const query = searchParams.toString();
    const href = `${pathname}${query ? `?${query}` : ''}`;
    const currentHref = window.sessionStorage.getItem(DASHBOARD_CURRENT_HREF_KEY);

    if (currentHref && currentHref !== href) {
      window.sessionStorage.setItem(DASHBOARD_PREVIOUS_HREF_KEY, currentHref);
    }

    window.sessionStorage.setItem(DASHBOARD_CURRENT_HREF_KEY, href);
  }, [pathname, searchParams]);

  return null;
}
