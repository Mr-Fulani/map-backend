/**
 * Шаг 5: Запуск первой синхронизации → готово.
 */

'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  ArrowLeft,
  Loader2,
  Rocket,
  CheckCircle2,
  PartyPopper,
  LayoutDashboard,
} from 'lucide-react';
import { datasourceApi } from '@/lib/api';
import { toast } from 'sonner';

interface StepSyncProps {
  data: Record<string, unknown>;
  onFinish: () => void;
  onBack: () => void;
}

type SyncStatus = 'idle' | 'syncing' | 'success' | 'error';

export function StepSync({ data, onFinish, onBack }: StepSyncProps) {
  const [syncStatus, setSyncStatus] = useState<SyncStatus>('idle');

  const datasourceId = data.datasource_id as number | undefined;
  const isCSV = (data.datasource_type as string) === 'csv';

  async function startSync() {
    setSyncStatus('syncing');

    // CSV: данные уже загружены, синхронизация не нужна
    if (isCSV || !datasourceId) {
      await new Promise((r) => setTimeout(r, 800)); // UX: короткая задержка
      setSyncStatus('success');
      toast.success('Настройка завершена!');
      return;
    }

    try {
      await datasourceApi.sync(datasourceId);
      setSyncStatus('success');
      toast.success('Синхронизация запущена!');
    } catch {
      setSyncStatus('error');
      toast.error('Ошибка запуска синхронизации');
    }
  }

  if (syncStatus === 'success') {
    return (
      <div className="space-y-6">
        <div className="flex flex-col items-center gap-4 py-8 text-center">
          <div className="relative">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-green-500/10">
              <CheckCircle2 className="h-8 w-8 text-green-500" />
            </div>
            <PartyPopper className="absolute -top-2 -right-2 h-6 w-6 text-yellow-500" />
          </div>
          <div>
            <h3 className="text-xl font-bold">Всё готово! 🎉</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              Синхронизация запущена. Ваши товары будут автоматически
              <br />
              публиковаться на Avito в течение ближайших минут.
            </p>
          </div>

          <div className="mt-4 w-full max-w-sm space-y-2 rounded-lg border bg-muted/30 p-4 text-left text-sm">
            <p className="font-medium">Что дальше:</p>
            <ul className="space-y-1 text-muted-foreground">
              <li>• Следите за статусом публикации в разделе «Листинги»</li>
              <li>• Настройте уведомления в Telegram</li>
              <li>• Синхронизация запускается автоматически каждые 5 минут</li>
            </ul>
          </div>
        </div>

        <div className="flex justify-center">
          <Button size="lg" onClick={onFinish}>
            <LayoutDashboard className="mr-2 h-4 w-4" />
            Перейти в дашборд
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col items-center gap-4 py-8 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
          <Rocket className="h-8 w-8 text-primary" />
        </div>
        <div>
          <h3 className="text-xl font-bold">Запустить синхронизацию?</h3>
          <p className="mt-2 text-sm text-muted-foreground">
            Мы начнём импорт товаров из вашего каталога
            <br />
            и автоматическую публикацию на Avito.
          </p>
        </div>

        {/* Summary */}
        <div className="w-full max-w-sm space-y-2 rounded-lg border p-4 text-left text-sm">
          <div className="flex justify-between">
            <span className="text-muted-foreground">Аккаунт Avito:</span>
            <span className="font-medium">{(data.avito_account_name as string) || '—'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">Источник данных:</span>
            <span className="font-medium">{(data.datasource_name as string) || '—'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">Тип:</span>
            <span className="font-medium">{(data.datasource_type as string) || '—'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">Категории:</span>
            <span className="font-medium">
              {data.categories_mapped ? 'Настроены' : 'Будут настроены позже'}
            </span>
          </div>
        </div>
      </div>

      <div className="flex justify-between">
        <Button type="button" variant="outline" onClick={onBack}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>
        <Button
          size="lg"
          onClick={startSync}
          disabled={syncStatus === 'syncing'}
        >
          {syncStatus === 'syncing' ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Запускаем...
            </>
          ) : (
            <>
              <Rocket className="mr-2 h-4 w-4" />
              Запустить синхронизацию
            </>
          )}
        </Button>
      </div>
    </div>
  );
}
