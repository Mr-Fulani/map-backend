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
  ClipboardCheck,
  Settings,
  CreditCard,
  Webhook,
  Images,
  Zap,
  Globe2,
  ShoppingBag,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const navItems = [
  { title: 'Дашборд', href: '/dashboard', icon: LayoutDashboard },
  { title: 'Товары', href: '/dashboard/products', icon: Package },
  { title: 'Листинги', href: '/dashboard/listings', icon: ListOrdered },
  { title: 'Заказы', href: '/dashboard/orders', icon: ShoppingBag },
  { title: 'Проверка', href: '/dashboard/review', icon: ClipboardCheck },
  { title: 'Интернет-поиск', href: '/dashboard/research', icon: Globe2 },
  { title: 'Медиа', href: '/dashboard/media', icon: Images },
  { title: 'Логи', href: '/dashboard/logs', icon: ScrollText },
  { title: 'Аналитика', href: '/dashboard/analytics', icon: BarChart3 },
  { title: 'Настройки', href: '/dashboard/settings', icon: Settings },
  { title: 'Биллинг', href: '/dashboard/billing', icon: CreditCard },
  { title: 'API & Webhooks', href: '/dashboard/api', icon: Webhook },
];

interface MobileSidebarProps {
  onNavigate?: () => void;
}

export function MobileSidebar({ onNavigate }: MobileSidebarProps) {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="flex h-14 items-center border-b px-4">
        <Link href="/dashboard" className="flex items-center gap-2.5" onClick={onNavigate}>
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
            <Zap className="h-4 w-4 text-primary-foreground" />
          </div>
          <span className="text-lg font-bold tracking-tight">MAP</span>
        </Link>
      </div>

      {/* Navigation */}
      <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto overscroll-contain px-2 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        {navItems.map((item) => {
          const isActive =
            pathname === item.href ||
            (item.href !== '/dashboard' && pathname.startsWith(item.href));

          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={onNavigate}
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
