'use client';

import { useEffect, useState } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import { cn } from '@/lib/utils';

function isModifiedClick(event: MouseEvent) {
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
}

function isDashboardNavigation(href: string) {
  try {
    const url = new URL(href, window.location.origin);
    const currentHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const nextHref = `${url.pathname}${url.search}${url.hash}`;

    return url.origin === window.location.origin
      && url.pathname.startsWith('/dashboard')
      && nextHref !== currentHref;
  } catch {
    return false;
  }
}

export function NavigationProgress() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [pending, setPending] = useState(false);

  useEffect(() => {
    const handleClick = (event: MouseEvent) => {
      if (isModifiedClick(event)) return;

      const target = event.target instanceof Element
        ? event.target.closest<HTMLAnchorElement>('a[href]')
        : null;

      if (target?.target || target?.hasAttribute('download')) return;
      if (target && isDashboardNavigation(target.href)) {
        setPending(true);
      }
    };

    document.addEventListener('click', handleClick, true);
    return () => document.removeEventListener('click', handleClick, true);
  }, []);

  useEffect(() => {
    setPending(false);
  }, [pathname, searchParams]);

  useEffect(() => {
    if (!pending) return undefined;

    const timer = window.setTimeout(() => setPending(false), 6000);
    return () => window.clearTimeout(timer);
  }, [pending]);

  return (
    <div
      className={cn(
        'pointer-events-none fixed inset-x-0 top-0 z-50 h-0.5 overflow-hidden bg-primary/10 opacity-0 transition-opacity duration-150',
        pending && 'opacity-100'
      )}
      aria-hidden="true"
    >
      <div className="h-full w-1/2 animate-dashboard-progress bg-primary shadow-[0_0_12px_hsl(var(--primary))]" />
    </div>
  );
}
