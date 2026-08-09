/**
 * Верхний header: tenant info, theme switcher, user menu.
 */

'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Moon, Sun, LogOut, User, Building2, Menu, ShieldX } from 'lucide-react';
import { useTheme } from 'next-themes';
import { toast } from 'sonner';
import { useAuth } from '@/lib/auth-context';
import { Button } from '@/components/ui/button';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Sheet,
  SheetContent,
  SheetTrigger,
} from '@/components/ui/sheet';
import { MobileSidebar } from './mobile-sidebar';

export function Header() {
  const { theme, setTheme } = useTheme();
  const { user, tenant, role, subscription, logout, logoutAll } = useAuth();
  const router = useRouter();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  async function handleLogout(everywhere = false) {
    if (isLoggingOut) return;
    setIsLoggingOut(true);
    try {
      await (everywhere ? logoutAll() : logout());
      router.replace('/login');
    } catch {
      toast.error(
        everywhere
          ? 'Не удалось отозвать все сессии. Повторите попытку.'
          : 'Не удалось завершить сессию на сервере. Повторите попытку.'
      );
    } finally {
      setIsLoggingOut(false);
    }
  }

  const initials = user?.email
    ? user.email.substring(0, 2).toUpperCase()
    : 'U';

  const planBadgeVariant =
    subscription?.status === 'active'
      ? 'default'
      : subscription?.status === 'trial'
        ? 'secondary'
        : 'destructive';

  const planStatusLabel =
    subscription?.status === 'past_due'
      ? ' (истекла)'
      : subscription?.status === 'cancelled'
        ? ' (отменена)'
      : subscription?.status === 'trial'
        ? ' (Trial)'
        : '';

  return (
    <header className="flex h-14 min-w-0 items-center justify-between gap-2 border-b bg-card px-3 sm:px-4 lg:px-6">
      {/* Left: Mobile menu + Tenant */}
      <div className="flex min-w-0 items-center gap-2 sm:gap-3">
        {/* Mobile menu */}
        <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon" className="lg:hidden">
              <Menu className="h-5 w-5" />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="w-[240px] p-0">
            <MobileSidebar onNavigate={() => setMobileMenuOpen(false)} />
          </SheetContent>
        </Sheet>

        {/* Tenant info */}
        {tenant && (
          <div className="flex min-w-0 items-center gap-2">
            <Building2 className="hidden h-4 w-4 shrink-0 text-muted-foreground sm:block" />
            <span className="min-w-0 truncate text-sm font-medium">{tenant.name}</span>
            {subscription && (
              <Badge variant={planBadgeVariant} className="hidden shrink-0 text-xs sm:inline-flex">
                {subscription.plan_name}{planStatusLabel}
              </Badge>
            )}
          </div>
        )}
      </div>

      {/* Right: Theme + User */}
      <div className="flex shrink-0 items-center gap-1 sm:gap-2">
        {/* Theme toggle */}
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
        >
          <Sun className="h-4 w-4 rotate-0 scale-100 transition-transform dark:-rotate-90 dark:scale-0" />
          <Moon className="absolute h-4 w-4 rotate-90 scale-0 transition-transform dark:rotate-0 dark:scale-100" />
          <span className="sr-only">Переключить тему</span>
        </Button>

        {/* User menu */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" className="relative h-8 w-8 rounded-full">
              <Avatar className="h-8 w-8">
                <AvatarFallback className="bg-primary/10 text-primary text-xs">
                  {initials}
                </AvatarFallback>
              </Avatar>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel>
              <div className="flex flex-col space-y-1">
                <p className="text-sm font-medium">{user?.email}</p>
                {role && (
                  <p className="text-xs text-muted-foreground capitalize">{role}</p>
                )}
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <Link href="/dashboard/settings#profile">
                <User className="mr-2 h-4 w-4" />
                Профиль
              </Link>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={() => void handleLogout()}
              disabled={isLoggingOut}
              className="text-destructive"
            >
              <LogOut className="mr-2 h-4 w-4" />
              Выйти
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => void handleLogout(true)}
              disabled={isLoggingOut}
              className="text-destructive"
            >
              <ShieldX className="mr-2 h-4 w-4" />
              Выйти на всех устройствах
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
