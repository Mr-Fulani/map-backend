'use client';

import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/lib/auth-context';

export function AuthRecovery() {
  const { isLoading, retrySession } = useAuth();

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="max-w-md space-y-4 text-center">
        <h1 className="text-xl font-semibold">Не удалось проверить сессию</h1>
        <p className="text-sm text-muted-foreground">
          Сессия не удалена. Проверьте соединение и повторите запрос.
        </p>
        <Button
          type="button"
          disabled={isLoading}
          onClick={() => void retrySession().catch(() => undefined)}
        >
          <RefreshCw className={isLoading ? 'mr-2 h-4 w-4 animate-spin' : 'mr-2 h-4 w-4'} />
          Повторить
        </Button>
      </div>
    </div>
  );
}
