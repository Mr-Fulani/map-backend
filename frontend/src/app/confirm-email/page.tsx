'use client';

import { useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { profileApi } from '@/lib/api';
import { CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

type State = 'loading' | 'success' | 'error';

export default function ConfirmEmailPage() {
  const searchParams = useSearchParams();
  const [state, setState] = useState<State>('loading');
  const [errorMessage, setErrorMessage] = useState('');

  useEffect(() => {
    const token = searchParams.get('token') ?? '';
    if (!token) {
      setState('error');
      setErrorMessage('Токен не найден. Проверьте ссылку из письма.');
      return;
    }

    profileApi.confirmEmail(token)
      .then(() => setState('success'))
      .catch((err: unknown) => {
        const msg = (err as { response?: { data?: { message?: string } } })?.response?.data?.message;
        setErrorMessage(msg ?? 'Ошибка подтверждения. Попробуйте запросить смену email заново.');
        setState('error');
      });
  }, [searchParams]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-md rounded-xl border bg-card p-8 text-center shadow-sm">
        {state === 'loading' && (
          <>
            <Loader2 className="mx-auto mb-4 h-12 w-12 animate-spin text-muted-foreground" />
            <p className="text-sm text-muted-foreground">Подтверждаем email...</p>
          </>
        )}

        {state === 'success' && (
          <>
            <CheckCircle2 className="mx-auto mb-4 h-12 w-12 text-green-500" />
            <h1 className="mb-2 text-xl font-bold">Email изменён</h1>
            <p className="mb-6 text-sm text-muted-foreground">
              Войдите заново с новым адресом.
            </p>
            <Button asChild className="w-full">
              <Link href="/login">Войти</Link>
            </Button>
          </>
        )}

        {state === 'error' && (
          <>
            <XCircle className="mx-auto mb-4 h-12 w-12 text-destructive" />
            <h1 className="mb-2 text-xl font-bold">Ошибка</h1>
            <p className="mb-6 text-sm text-muted-foreground">{errorMessage}</p>
            <Button asChild variant="outline" className="w-full">
              <Link href="/dashboard/settings#profile">Вернуться в настройки</Link>
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
