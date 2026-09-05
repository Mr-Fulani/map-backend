'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { accountApi } from '@/lib/api';
import type { OzonSyncHealth as Health, OzonSyncState } from '@/lib/marketplace-account-types';

const STATES: Record<OzonSyncState, { label: string; className: string }> = {
  disabled: { label: 'Выключено', className: 'border-border bg-muted/30' },
  idle: { label: 'Нет задач', className: 'border-border bg-muted/30' },
  unknown: { label: 'Нет данных', className: 'border-border bg-muted/30' },
  pending: { label: 'Ожидаем результат', className: 'border-blue-500/30 bg-blue-500/5' },
  ok: { label: 'Успешный запуск', className: 'border-green-500/30 bg-green-500/5' },
  error: { label: 'Ошибка', className: 'border-red-500/30 bg-red-500/5' },
  delayed: { label: 'Задержка', className: 'border-amber-500/30 bg-amber-500/5' },
};

function dateLabel(value: string | null) {
  return value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString('ru-RU') : 'Пока нет';
}

export function OzonSyncHealth({ accountId, initial }: { accountId: number; initial?: Health }) {
  const [health, setHealth] = useState(initial ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') setNow(Date.now());
    }, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  async function refresh() {
    setLoading(true);
    setError(false);
    try {
      const response = await accountApi.getOzonHealth(accountId);
      setHealth(response.data.data as Health);
      setNow(Date.now());
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }

  const screenStale = health && now - Date.parse(health.observed_at) > 120_000;
  return (
    <section aria-label="Состояние синхронизации Ozon" className="space-y-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">Состояние синхронизации</p>
          <p className="text-xs text-muted-foreground">Данные на {dateLabel(health?.observed_at ?? null)}</p>
        </div>
        <Button size="sm" variant="outline" disabled={loading} onClick={refresh}>
          <RefreshCw className={`mr-2 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          {loading ? 'Проверяем…' : 'Обновить диагностику'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">Обновление диагностики не отправляет товары, цены или остатки в Ozon.</p>
      {error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">Не удалось обновить диагностику. Последний снимок может быть устаревшим.</p>}
      {screenStale && <p className="text-sm text-amber-700 dark:text-amber-400">Экран давно не обновлялся. Обновите диагностику перед проверкой состояния.</p>}
      {!health ? <p className="text-sm text-muted-foreground">Настройки изменились. Обновите диагностику, чтобы увидеть новое состояние.</p> : <>
        {health.monitor_status !== 'ok' && <p className="text-sm text-amber-700 dark:text-amber-400">Нет свежей отметки фонового монитора. Успешная работа сейчас не подтверждена.</p>}
        {health.queue_status !== 'available' && <p className="text-xs text-muted-foreground">
          {health.queue_status === 'unavailable' ? 'Не найден обработчик очереди синхронизации. MAP требуется проверка.' : 'Нет свежего подтверждения доступности фоновой очереди. Это не означает ошибку карточки Ozon.'}
        </p>}
        {health.credential.code && <p className="text-sm text-amber-700 dark:text-amber-400">{health.credential.message}</p>}
        <div className="grid gap-2 xl:grid-cols-3">
          {health.jobs.map((job) => {
            const presentation = STATES[job.state] ?? STATES.unknown;
            return <div key={job.job} className={`space-y-1 rounded-md border p-3 ${presentation.className}`}>
              <p className="text-sm font-medium">{job.label}</p>
              <p className="text-sm font-semibold">{presentation.label}</p>
              <p className="text-xs">{job.message}</p>
              <p className="text-xs text-muted-foreground">Последний успешный запуск: {dateLabel(job.last_success_at)}</p>
              {job.state === 'error' && !job.alert_ready && <p className="text-xs text-muted-foreground">MAP проверит, повторяется ли сбой, прежде чем отправить уведомление.</p>}
            </div>;
          })}
        </div>
      </>}
      <p className="text-xs text-muted-foreground">Об устойчивом сбое и восстановлении сообщим в подключённый Telegram, если уведомления об ошибках включены. Повторы одного инцидента не отправляются.</p>
      <Link href="/dashboard/settings#notifications" className="text-xs underline">Настройки уведомлений</Link>
    </section>
  );
}
