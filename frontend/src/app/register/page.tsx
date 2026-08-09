/**
 * Страница регистрации — создание тенанта и пользователя.
 * После регистрации → redirect на настройки.
 */

'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { authApi } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Zap, Loader2 } from 'lucide-react';
import { toast } from 'sonner';

function flattenErrorMessages(value: unknown): string[] {
  if (!value) return [];
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(flattenErrorMessages);
  if (typeof value === 'object') return Object.values(value).flatMap(flattenErrorMessages);
  return [String(value)];
}

const CYRILLIC_TO_LATIN: Record<string, string> = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z',
  и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r',
  с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'c', ч: 'ch', ш: 'sh',
  щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
};

function makeAsciiSlug(value: string) {
  return value
    .toLowerCase()
    .split('')
    .map((char) => CYRILLIC_TO_LATIN[char] ?? char)
    .join('')
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .substring(0, 50);
}

export default function RegisterPage() {
  const [form, setForm] = useState({
    name: '',
    slug: '',
    email: '',
    password: '',
  });
  const [isLoading, setIsLoading] = useState(false);
  const { login, isLoading: isAuthLoading } = useAuth();
  const router = useRouter();

  function handleChange(field: string, value: string) {
    setForm((prev) => ({ ...prev, [field]: value }));
    // Автогенерация slug из name
    if (field === 'name') {
      const slug = makeAsciiSlug(value);
      setForm((prev) => ({ ...prev, slug }));
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (isAuthLoading) return;
    setIsLoading(true);

    try {
      // 1. Регистрируем тенант
      await authApi.register(form);

      // 2. Создаём browser-сессию: refresh остаётся только в HttpOnly cookie.
      await login(form.email, form.password, form.slug);

      toast.success('Регистрация успешна! Давайте настроим вашу платформу.');
      router.push('/dashboard/settings');
    } catch (err: unknown) {
      const errorData = (err as { response?: { data?: Record<string, unknown> } })?.response?.data;
      const messages = flattenErrorMessages(errorData?.message ?? errorData?.detail ?? errorData?.errors ?? errorData);
      const message = messages.length > 0 ? messages.join('. ') : 'Ошибка регистрации';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-background via-background to-muted/50 px-4">
      {/* Decorative elements */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-40 -right-40 h-80 w-80 rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute -bottom-40 -left-40 h-80 w-80 rounded-full bg-primary/5 blur-3xl" />
      </div>

      <Card className="relative w-full max-w-md">
        <CardHeader className="text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-primary shadow-lg shadow-primary/25">
            <Zap className="h-6 w-6 text-primary-foreground" />
          </div>
          <CardTitle className="text-2xl">Регистрация в MAP</CardTitle>
          <CardDescription>
            Создайте аккаунт и начните автоматизацию за 5 минут
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="name">Название компании</Label>
              <Input
                id="name"
                placeholder="ООО Автозапчасти"
                value={form.name}
                onChange={(e) => handleChange('name', e.target.value)}
                maxLength={200}
                required
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="slug">URL-идентификатор</Label>
              <div className="flex items-center gap-1">
                <Input
                  id="slug"
                  placeholder="avtozapchasti"
                  value={form.slug}
                  onChange={(e) => handleChange('slug', makeAsciiSlug(e.target.value))}
                  maxLength={50}
                  required
                  className="font-mono text-sm"
                />
              </div>
              <p className="text-xs text-muted-foreground">
                Только английские буквы, цифры и дефисы: map.domain.ru/t/{form.slug || 'your-slug'}/
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="reg-email">Email</Label>
              <Input
                id="reg-email"
                type="email"
                placeholder="user@company.ru"
                value={form.email}
                onChange={(e) => handleChange('email', e.target.value)}
                maxLength={254}
                required
                autoComplete="email"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="reg-password">Пароль</Label>
              <PasswordInput
                id="reg-password"
                placeholder="Минимум 12 символов"
                value={form.password}
                onChange={(e) => handleChange('password', e.target.value)}
                maxLength={256}
                required
                minLength={12}
                autoComplete="new-password"
              />
            </div>
            <Button type="submit" className="w-full" disabled={isLoading || isAuthLoading}>
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Создание...
                </>
              ) : (
                'Создать аккаунт'
              )}
            </Button>
          </form>
          <div className="mt-4 text-center text-xs text-muted-foreground">
            14 дней бесплатно • План Business • Без ввода карты
          </div>
          <div className="mt-4 text-center text-sm text-muted-foreground">
            Уже есть аккаунт?{' '}
            <Link href="/login" className="text-primary hover:underline">
              Войти
            </Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
