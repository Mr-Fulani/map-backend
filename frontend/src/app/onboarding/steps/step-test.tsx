/**
 * Шаг 3: Тест подключения и импорт товаров.
 * Для CSV — автоматически проходит (файл уже загружен).
 * Для 1С — тестируем HTTP-соединение.
 */

'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { datasourceApi } from '@/lib/api';
import {
  ArrowLeft,
  ArrowRight,
  Loader2,
  CheckCircle2,
  XCircle,
  RefreshCw,
  FileCheck,
} from 'lucide-react';
import { toast } from 'sonner';

interface StepTestProps {
  data: Record<string, unknown>;
  onNext: (data?: Record<string, unknown>) => void;
  onBack: () => void;
}

type TestStatus = 'idle' | 'testing' | 'success' | 'error';

export function StepTest({ data, onNext, onBack }: StepTestProps) {
  const [testStatus, setTestStatus] = useState<TestStatus>('idle');
  const [testMessage, setTestMessage] = useState('');

  const datasourceType = data.datasource_type as string;
  const datasourceId = data.datasource_id as number | undefined;
  const isCSV = datasourceType === 'csv';
  const status: TestStatus = isCSV ? 'success' : testStatus;
  const message = isCSV ? 'CSV-файл успешно загружен и проверен' : testMessage;

  async function runTest() {
    if (!datasourceId) {
      setTestStatus('error');
      setTestMessage('Источник данных не найден');
      return;
    }
    setTestStatus('testing');
    try {
      const { data: result } = await datasourceApi.test(datasourceId);
      if (result.ok) {
        setTestStatus('success');
        setTestMessage('Подключение успешно!');
        toast.success('Тест подключения пройден');
      } else {
        setTestStatus('error');
        setTestMessage(result.error || 'Не удалось подключиться');
        toast.error('Тест не пройден');
      }
    } catch (err: unknown) {
      setTestStatus('error');
      const axiosErr = err as { response?: { data?: { detail?: string; message?: string } } };
      const message =
        axiosErr?.response?.data?.detail ||
        axiosErr?.response?.data?.message ||
        'Ошибка подключения к источнику данных';
      setTestMessage(message);
      toast.error(message);
    }
  }

  const canProceed = status === 'success';

  return (
    <div className="space-y-6">
      <div className="space-y-4">
        <div className="flex items-center justify-between rounded-lg border p-4">
          <div className="flex items-center gap-3">
            {status === 'idle' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full border-2 border-dashed">
                <RefreshCw className="h-5 w-5 text-muted-foreground" />
              </div>
            )}
            {status === 'testing' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
                <Loader2 className="h-5 w-5 animate-spin text-primary" />
              </div>
            )}
            {status === 'success' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-green-500/10">
                <CheckCircle2 className="h-5 w-5 text-green-500" />
              </div>
            )}
            {status === 'error' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-destructive/10">
                <XCircle className="h-5 w-5 text-destructive" />
              </div>
            )}
            <div>
              <p className="text-sm font-medium">
                {isCSV ? 'Проверка файла' : 'Тест подключения'}
              </p>
              <p className="text-xs text-muted-foreground">
                {status === 'idle' && `Проверяем доступность ${data.datasource_name || 'источника'}`}
                {status === 'testing' && 'Подключаемся...'}
                {status === 'success' && message}
                {status === 'error' && message}
              </p>
            </div>
          </div>
          {!isCSV && (
            <Button
              variant={status === 'error' ? 'destructive' : 'outline'}
              size="sm"
              onClick={runTest}
              disabled={status === 'testing'}
            >
              {status === 'testing' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : status === 'error' ? (
                'Повторить'
              ) : status === 'success' ? (
                'Повторить'
              ) : (
                'Запустить тест'
              )}
            </Button>
          )}
          {isCSV && status === 'success' && (
            <FileCheck className="h-5 w-5 text-green-500" />
          )}
        </div>
      </div>

      <div className="flex justify-between">
        <Button type="button" variant="outline" onClick={onBack}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>
        <Button onClick={() => onNext({ test_passed: true })} disabled={!canProceed}>
          Продолжить
          <ArrowRight className="ml-2 h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
