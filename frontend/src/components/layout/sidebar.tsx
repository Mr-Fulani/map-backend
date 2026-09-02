/**
 * Sidebar навигация для dashboard.
 * Адаптивная: на мобильных — Sheet, на десктопе — фиксированная панель.
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
  ChevronLeft,
  ChevronRight,
  Zap,
  Globe2,
  ShoppingBag,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { useState } from 'react';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';

const navItems = [
  {
    title: 'Дашборд',
    href: '/dashboard',
    icon: LayoutDashboard,
  },
  {
    title: 'Товары',
    href: '/dashboard/products',
    icon: Package,
  },
  {
    title: 'Листинги',
    href: '/dashboard/listings',
    icon: ListOrdered,
  },
  {
    title: 'Заказы',
    href: '/dashboard/orders',
    icon: ShoppingBag,
  },
  {
    title: 'Проверка',
    href: '/dashboard/review',
    icon: ClipboardCheck,
  },
  {
    title: 'Интернет-поиск',
    href: '/dashboard/research',
    icon: Globe2,
  },
  {
    title: 'Медиа',
    href: '/dashboard/media',
    icon: Images,
  },
  {
    title: 'Логи',
    href: '/dashboard/logs',
    icon: ScrollText,
  },
  {
    title: 'Аналитика',
    href: '/dashboard/analytics',
    icon: BarChart3,
  },
  {
    title: 'Настройки',
    href: '/dashboard/settings',
    icon: Settings,
  },
  {
    title: 'Биллинг',
    href: '/dashboard/billing',
    icon: CreditCard,
  },
  {
    title: 'API & Webhooks',
    href: '/dashboard/api',
    icon: Webhook,
  },
];

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <aside
      className={cn(
        'relative hidden h-screen flex-col border-r bg-card transition-all duration-300 lg:flex',
        collapsed ? 'w-[68px]' : 'w-[240px]'
      )}
    >
      {/* Logo */}
      <div className="flex h-14 items-center border-b px-4">
        <Link href="/dashboard" className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
            <Zap className="h-4 w-4 text-primary-foreground" />
          </div>
          {!collapsed && (
            <span className="text-lg font-bold tracking-tight">MAP</span>
          )}
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 px-2 py-3">
        {navItems.map((item) => {
          const isActive =
            pathname === item.href ||
            (item.href !== '/dashboard' && pathname.startsWith(item.href));

          const linkContent = (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                collapsed && 'justify-center px-2'
              )}
            >
              <item.icon className="h-4 w-4 shrink-0" />
              {!collapsed && <span>{item.title}</span>}
            </Link>
          );

          if (collapsed) {
            return (
              <Tooltip key={item.href} delayDuration={0}>
                <TooltipTrigger asChild>{linkContent}</TooltipTrigger>
                <TooltipContent side="right" sideOffset={8}>
                  {item.title}
                </TooltipContent>
              </Tooltip>
            );
          }

          return linkContent;
        })}
      </nav>

      {/* Collapse toggle */}
      <div className="border-t p-2">
        <Button
          variant="ghost"
          size="icon"
          className="w-full"
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? (
            <ChevronRight className="h-4 w-4" />
          ) : (
            <ChevronLeft className="h-4 w-4" />
          )}
        </Button>
      </div>
    </aside>
  );
}
