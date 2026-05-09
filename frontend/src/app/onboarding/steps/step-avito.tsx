/**
 * Шаг 1: Подключение аккаунта Avito.
 * Пользователь вводит client_id и client_secret из личного кабинета Avito.
 */

'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { accountApi } from '@/lib/api';
import { ArrowRight, Loader2, ExternalLink, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';

interface StepAvitoProps {
  data: Record<string, unknown>;
  onNext: (data?: Record<string, unknown>) => void;
}

export function StepAvito({ data, onNext }: StepAvitoProps) {
  const [clientId, setClientId] = useState((data.avito_client_id as string) || '');
  const [clientSecret, setClientSecret] = useState((data.avito_client_secret as string) || '');
  const [accountName, setAccountName] = useState((data.avito_account_name as string) || 'Основной аккаунт');
  const [isLoading, setIsLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);

    try {
      const { data: result } = await accountApi.create({
        marketplace: 'avito',
        name: accountName,
        external_id: clientId,
        client_id: clientId,
        client_secret: clientSecret,
      });

      toast.success('Аккаунт Avito подключён!');
      onNext({
        avito_client_id: clientId,
        avito_client_secret: clientSecret,
        avito_account_name: accountName,
        avito_account_id: result.id,
      });
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status?: number; data?: Record<string, unknown> } };
      // 409 = аккаунт уже создан ранее — это ок, идём дальше
      if (axiosErr?.response?.status === 409) {
        toast.success('Аккаунт Avito уже подключён!');
        onNext({
          avito_client_id: clientId,
          avito_client_secret: clientSecret,
          avito_account_name: accountName,
        });
        return;
      }
      const respData = axiosErr?.response?.data;
      const message =
        (respData?.detail as string) ||
        (respData?.message as string) ||
        JSON.stringify(respData) ||
        'Не удалось подключить аккаунт Avito. Проверьте данные.';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Instructions */}
      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-4">
        <div className="flex items-start gap-3">
          <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-blue-500" />
          <div className="space-y-1 text-sm">
            <p className="font-medium text-blue-500">Как получить API-ключи Avito</p>
            <ol className="list-inside list-decimal space-y-1 text-muted-foreground">
              <li>
                Перейдите в{' '}
                <a
                  href="https://www.avito.ru/professionals/api"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-blue-500 hover:underline"
                >
                  Avito для бизнеса <ExternalLink className="h-3 w-3" />
                </a>
              </li>
              <li>Создайте приложение, если ещё не создано</li>
              <li>Скопируйте Client ID и Client Secret</li>
            </ol>
          </div>
        </div>
      </div>

      <div className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="account-name">Название аккаунта</Label>
          <Input
            id="account-name"
            placeholder="Основной аккаунт"
            value={accountName}
            onChange={(e) => setAccountName(e.target.value)}
            required
          />
          <p className="text-xs text-muted-foreground">
            Для вашего удобства — если у вас несколько аккаунтов Avito
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="client-id">Client ID</Label>
          <Input
            id="client-id"
            placeholder="Вставьте Client ID из Avito"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            required
            className="font-mono"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="client-secret">Client Secret</Label>
          <Input
            id="client-secret"
            type="password"
            placeholder="Вставьте Client Secret из Avito"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            required
            className="font-mono"
          />
          <p className="text-xs text-muted-foreground">
            Ключи хранятся в зашифрованном виде и не передаются третьим лицам
          </p>
        </div>
      </div>

      <div className="flex justify-end">
        <Button type="submit" disabled={isLoading || !clientId || !clientSecret}>
          {isLoading ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Подключение...
            </>
          ) : (
            <>
              Подключить и продолжить
              <ArrowRight className="ml-2 h-4 w-4" />
            </>
          )}
        </Button>
      </div>
    </form>
  );
}
