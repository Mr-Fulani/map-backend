/**
 * Onboarding — короткое завершение регистрации.
 * Интеграции Avito и источники данных настраиваются в дашборде.
 */

'use client';

import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth-context';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { ArrowRight, Database, Settings, Store, Zap } from 'lucide-react';

export default function OnboardingPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading, tenant } = useAuth();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (!isAuthenticated) {
    router.push('/login');
    return null;
  }

  return (
    <div className="flex min-h-screen flex-col bg-gradient-to-br from-background via-background to-muted/30">
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-60 left-1/2 h-[500px] w-[500px] -translate-x-1/2 rounded-full bg-primary/3 blur-3xl" />
      </div>

      <header className="relative border-b bg-card/50 backdrop-blur-sm">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
              <Zap className="h-4 w-4 text-primary-foreground" />
            </div>
            <span className="text-lg font-bold">MAP</span>
          </div>
          <div className="text-sm text-muted-foreground">
            Регистрация завершена
          </div>
        </div>
      </header>

      <main className="relative mx-auto flex w-full max-w-3xl flex-1 items-center px-4 py-8">
        <Card className="w-full">
          <CardHeader>
            <CardTitle>Аккаунт готов</CardTitle>
            <CardDescription>
              {tenant?.name
                ? `${tenant.name} создан. Подключения можно настроить в дашборде, когда будет удобно.`
                : 'Подключения можно настроить в дашборде, когда будет удобно.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border p-4">
                <Settings className="mb-3 h-5 w-5 text-primary" />
                <p className="text-sm font-medium">Профиль и команда</p>
                <p className="mt-1 text-xs text-muted-foreground">Основные настройки аккаунта.</p>
              </div>
              <div className="rounded-lg border p-4">
                <Store className="mb-3 h-5 w-5 text-primary" />
                <p className="text-sm font-medium">Avito-аккаунты</p>
                <p className="mt-1 text-xs text-muted-foreground">Подключение вынесено в настройки.</p>
              </div>
              <div className="rounded-lg border p-4">
                <Database className="mb-3 h-5 w-5 text-primary" />
                <p className="text-sm font-medium">Источники данных</p>
                <p className="mt-1 text-xs text-muted-foreground">1C и файлы добавляются отдельно.</p>
              </div>
            </div>

            <div className="flex flex-col gap-3 sm:flex-row sm:justify-end">
              <Button variant="outline" onClick={() => router.push('/dashboard/settings')}>
                Открыть настройки
              </Button>
              <Button onClick={() => router.push('/dashboard')}>
                Перейти в дашборд
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            </div>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
