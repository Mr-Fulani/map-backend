/**
 * Шаг 2: Активация Avito Автозагрузки.
 *
 * Avito Автозагрузка — сервис для публикации объявлений через API.
 * Пользователь должен активировать его один раз в своём кабинете Avito и
 * указать там URL нашего фида. Без этого публикация невозможна, но шаг
 * можно пропустить и вернуться к настройке позже.
 */

'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { accountApi } from '@/lib/api';
import {
  ArrowRight,
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  Loader2,
  AlertCircle,
  Copy,
  Check,
} from 'lucide-react';
import { toast } from 'sonner';

interface AutoloadOnboarding {
  state: string;
  ready: boolean;
  retryable: boolean;
  message: string;
}

interface StepAutoloadProps {
  data: Record<string, unknown>;
  onNext: (data?: Record<string, unknown>) => void;
  onBack: () => void;
}

export function StepAutoload({ data, onNext, onBack }: StepAutoloadProps) {
  const accountId = data.avito_account_id as number | undefined;
  const [isChecking, setIsChecking] = useState(false);
  const [activated, setActivated] = useState(false);
  const [feedUrl, setFeedUrl] = useState<string>('');
  const [feedEndpointManaged, setFeedEndpointManaged] = useState(
    Boolean(data.avito_feed_endpoint_managed),
  );
  const [onboarding, setOnboarding] = useState<AutoloadOnboarding | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);
  const [copied, setCopied] = useState(false);

  async function handleCheck() {
    if (!accountId) {
      toast.error('Сначала подключите аккаунт Avito на предыдущем шаге');
      return;
    }
    setIsChecking(true);
    try {
      const { data: result } = await accountApi.checkAutoload(accountId);
      setFeedUrl(result.feed_url || '');
      setFeedEndpointManaged(Boolean(result.feed_endpoint_managed));
      setOnboarding(result.autoload_onboarding ?? null);
      const endpointReady = (
        !result.feed_endpoint_managed
        || result.autoload_onboarding?.ready === true
      );
      if (result.activated && endpointReady) {
        setActivated(true);
        toast.success('Автозагрузка и фид MAP готовы!');
      } else if (result.activated) {
        setActivated(false);
        toast.warning('Avito включён, но MAP ещё завершает настройку защищённого фида.');
      } else {
        setActivated(false);
        toast.error('Автозагрузка ещё не активирована. Выполните шаги ниже и проверьте снова.');
      }
    } catch {
      toast.error('Не удалось проверить статус. Попробуйте ещё раз.');
    } finally {
      setIsChecking(false);
    }
  }

  async function retryOnboarding() {
    if (!accountId) return;
    setIsRetrying(true);
    try {
      const { data: result } = await accountApi.retryAutoload(accountId);
      setOnboarding(result);
      toast.success('Повторная настройка фида поставлена в очередь');
    } catch (error: unknown) {
      const response = (error as { response?: { data?: AutoloadOnboarding } }).response;
      toast.error(response?.data?.message || 'Не удалось повторить настройку фида');
    } finally {
      setIsRetrying(false);
    }
  }

  async function copyFeedUrl() {
    if (!feedUrl) return;
    await navigator.clipboard.writeText(feedUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function handleSkip() {
    toast.info('Автозагрузку можно подключить позже в настройках.');
    onNext({ autoload_skipped: true, autoload_activated: false });
  }

  return (
    <div className="space-y-6">
      {/* Объяснение */}
      <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
        <div className="flex items-start gap-3">
          <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
          <div className="space-y-1 text-sm">
            <p className="font-medium text-amber-500">Требуется одно действие в кабинете Avito</p>
            <p className="text-muted-foreground">
              Avito Автозагрузка — это сервис, через который наша платформа публикует ваши
              объявления. Его можно активировать сейчас или позже в настройках.
            </p>
          </div>
        </div>
      </div>

      {/* Инструкция */}
      <div className="space-y-4">
        <p className="text-sm font-medium">
          {feedEndpointManaged
            ? 'Проверьте Автозагрузку в кабинете Avito:'
            : 'Выполните 3 шага в кабинете Avito:'}
        </p>

        <ol className="space-y-4">
          {/* Шаг 1 */}
          <li className="flex gap-3">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
              1
            </span>
            <div className="space-y-2">
              <p className="text-sm">
                Откройте настройки Автозагрузки в вашем аккаунте Avito:
              </p>
              <a
                href="https://www.avito.ru/autoload/settings"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
              >
                Открыть настройки Авито
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            </div>
          </li>

          {/* Шаг 2 */}
          <li className="flex gap-3">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
              2
            </span>
            <div className="space-y-2 min-w-0 flex-1">
              <p className="text-sm">
                {feedEndpointManaged && onboarding?.ready ? (
                  <>
                    MAP уже добавил защищённую ссылку фида автоматически.
                    Включите Автозагрузку — копировать URL не нужно.
                  </>
                ) : feedEndpointManaged ? (
                  <>
                    MAP настраивает защищённую ссылку фида автоматически.
                    Копировать URL не нужно; дождитесь подтверждения ниже.
                  </>
                ) : (
                  <>
                    Включите Автозагрузку и вставьте этот URL в поле{' '}
                    <strong>«Ссылка на файл»</strong>:
                  </>
                )}
              </p>
              {!feedEndpointManaged && (feedUrl ? (
                <div className="flex items-center gap-2 rounded-md border bg-muted/50 px-3 py-2">
                  <code className="flex-1 truncate text-xs">{feedUrl}</code>
                  <button
                    onClick={copyFeedUrl}
                    className="shrink-0 text-muted-foreground hover:text-foreground transition-colors"
                    title="Скопировать"
                  >
                    {copied ? (
                      <Check className="h-4 w-4 text-green-500" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                  </button>
                </div>
              ) : (
                <div className="rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
                  URL загрузится после нажатия «Проверить» ниже
                </div>
              ))}
            </div>
          </li>

          {/* Шаг 3 */}
          <li className="flex gap-3">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
              3
            </span>
            <p className="text-sm">
              Нажмите <strong>«Сохранить»</strong> в настройках Avito и вернитесь сюда.
            </p>
          </li>
        </ol>
      </div>

      {/* Статус */}
      {feedEndpointManaged && onboarding && !onboarding.ready && (
        <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
          <div className="flex items-start gap-3">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-amber-600">
                Защищённый фид MAP ещё не готов
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{onboarding.message}</p>
              {onboarding.retryable && (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-3"
                  onClick={retryOnboarding}
                  disabled={isRetrying}
                >
                  {isRetrying && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Повторить настройку MAP
                </Button>
              )}
            </div>
          </div>
        </div>
      )}

      {activated && (
        <div className="flex items-center gap-3 rounded-lg border border-green-500/20 bg-green-500/5 p-4">
          <CheckCircle2 className="h-5 w-5 shrink-0 text-green-500" />
          <p className="text-sm font-medium text-green-500">
            Автозагрузка Avito и защищённый фид MAP готовы. Можно продолжать.
          </p>
        </div>
      )}

      {/* Кнопки */}
      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
        <Button variant="ghost" onClick={onBack} className="w-full sm:w-auto">
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>

        <div className="grid gap-2 sm:flex sm:gap-3">
          {!activated && (
            <Button variant="ghost" onClick={handleSkip}>
              Настроить позже
            </Button>
          )}

          {!activated && (
            <Button variant="outline" onClick={handleCheck} disabled={isChecking}>
              {isChecking ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Проверяю...
                </>
              ) : (
                'Проверить активацию'
              )}
            </Button>
          )}

          <Button
            onClick={() => onNext({ autoload_skipped: false, autoload_activated: true })}
            disabled={!activated}
          >
            Продолжить
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
