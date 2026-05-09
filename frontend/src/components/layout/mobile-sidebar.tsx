/**
 * Мобильная версия сайдбара для Sheet.
 */

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutDashboard,
  Package,
  ListOrdered,
  ScrollText,
  BarChart3,
  Settings,
  CreditCard,
  Webhook,
  Zap,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const navItems = [
  { title: 'Дашборд', href: '/dashboard', icon: LayoutDashboard },
  { title: 'Товары', href: '/dashboard/products', icon: Package },
  { title: 'Листинги', href: '/dashboard/listings', icon: ListOrdered },
  { title: 'Логи', href: '/dashboard/logs', icon: ScrollText },
  { title: 'Аналитика', href: '/dashboard/analytics', icon: BarChart3 },
  { title: 'Настройки', href: '/dashboard/settings', icon: Settings },
  { title: 'Биллинг', href: '/dashboard/billing', icon: CreditCard },
  { title: 'API & Webhooks', href: '/dashboard/api', icon: Webhook },
];

export function MobileSidebar() {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="flex h-14 items-center border-b px-4">
        <Link href="/dashboard" className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
            <Zap className="h-4 w-4 text-primary-foreground" />
          </div>
          <span className="text-lg font-bold tracking-tight">MAP</span>
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 px-2 py-3">
        {navItems.map((item) => {
          const isActive =
            pathname === item.href ||
            (item.href !== '/dashboard' && pathname.startsWith(item.href));

          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              )}
            >
              <item.icon className="h-4 w-4 shrink-0" />
              <span>{item.title}</span>
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
