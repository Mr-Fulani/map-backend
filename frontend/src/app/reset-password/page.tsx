'use client';

import { useLayoutEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { KeyRound, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { authApi } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { PasswordInput } from '@/components/ui/password-input';

interface ResetCredentials {
  uid: string;
  token: string;
}

export default function ResetPasswordPage() {
  const router = useRouter();
  const [credentials, setCredentials] = useState<ResetCredentials | null>(null);
  const [isReady, setIsReady] = useState(false);
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const fragmentConsumed = useRef(false);

  useLayoutEffect(() => {
    if (fragmentConsumed.current) return;
    fragmentConsumed.current = true;
    const currentUrl = new URL(window.location.href);
    const fragment = new URLSearchParams(currentUrl.hash.replace(/^#/, ''));
    const uid = fragment.get('uid') ?? '';
    const token = fragment.get('token') ?? '';

    // Remove reset secrets from the address bar and current history entry before rendering the form.
    window.history.replaceState(window.history.state, '', currentUrl.pathname);
    setCredentials(uid && token ? { uid, token } : null);
    setIsReady(true);
  }, []);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!credentials) return;
    if (password !== confirmation) {
      toast.error('Пароли не совпадают');
      return;
    }

    setIsLoading(true);
    try {
      await authApi.resetPassword({
        uid: credentials.uid,
        token: credentials.token,
        new_password: password,
      });
      setCredentials(null);
      setPassword('');
      setConfirmation('');
      toast.success('Пароль изменён. Теперь войдите с новым паролем.');
      router.replace('/login');
    } catch (error: unknown) {
      const response = (error as {
        response?: { data?: { message?: string; detail?: string } };
      }).response?.data;
      toast.error(response?.message ?? response?.detail ?? 'Ссылка недействительна или устарела');
    } finally {
      setIsLoading(false);
    }
  }

  if (!isReady) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-background via-background to-muted/50 px-4">
      <Card className="relative w-full max-w-md">
        <CardHeader className="text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-primary shadow-lg shadow-primary/25">
            <KeyRound className="h-6 w-6 text-primary-foreground" />
          </div>
          <CardTitle className="text-2xl">Новый пароль</CardTitle>
          <CardDescription>
            {credentials
              ? 'Придумайте новый пароль для аккаунта.'
              : 'Ссылка для восстановления неполная или недействительна.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {credentials ? (
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="reset-password">Новый пароль</Label>
                <PasswordInput
                  id="reset-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  maxLength={256}
                  placeholder="Минимум 12 символов"
                  autoComplete="new-password"
                  minLength={12}
                  required
                  autoFocus
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="reset-password-confirmation">Повторите пароль</Label>
                <PasswordInput
                  id="reset-password-confirmation"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  maxLength={256}
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </div>
              <Button type="submit" className="w-full" disabled={isLoading}>
                {isLoading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Сохранение...
                  </>
                ) : (
                  'Сохранить пароль'
                )}
              </Button>
            </form>
          ) : (
            <Button asChild className="w-full">
              <Link href="/forgot-password">Запросить новую ссылку</Link>
            </Button>
          )}
          <div className="mt-5 text-center text-sm text-muted-foreground">
            <Link href="/login" className="text-primary hover:underline">
              Вернуться ко входу
            </Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
