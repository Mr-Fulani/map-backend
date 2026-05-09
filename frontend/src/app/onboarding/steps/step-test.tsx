/**
 * Шаг 3: Тест подключения и импорт товаров.
 * Для CSV — автоматически проходит (файл уже загружен).
 * Для 1С — тестируем HTTP-соединение.
 */

'use client';

import { useState, useEffect } from 'react';
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

  // CSV не требует тест подключения — файл уже загружен и распарсен
  useEffect(() => {
    if (isCSV) {
      setTestStatus('success');
      setTestMessage('CSV-файл успешно загружен и проверен');
    }
  }, [isCSV]);

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

  const canProceed = testStatus === 'success';

  return (
    <div className="space-y-6">
      <div className="space-y-4">
        <div className="flex items-center justify-between rounded-lg border p-4">
          <div className="flex items-center gap-3">
            {testStatus === 'idle' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full border-2 border-dashed">
                <RefreshCw className="h-5 w-5 text-muted-foreground" />
              </div>
            )}
            {testStatus === 'testing' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
                <Loader2 className="h-5 w-5 animate-spin text-primary" />
              </div>
            )}
            {testStatus === 'success' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-green-500/10">
                <CheckCircle2 className="h-5 w-5 text-green-500" />
              </div>
            )}
            {testStatus === 'error' && (
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-destructive/10">
                <XCircle className="h-5 w-5 text-destructive" />
              </div>
            )}
            <div>
              <p className="text-sm font-medium">
                {isCSV ? 'Проверка файла' : 'Тест подключения'}
              </p>
              <p className="text-xs text-muted-foreground">
                {testStatus === 'idle' && `Проверяем доступность ${data.datasource_name || 'источника'}`}
                {testStatus === 'testing' && 'Подключаемся...'}
                {testStatus === 'success' && testMessage}
                {testStatus === 'error' && testMessage}
              </p>
            </div>
          </div>
          {!isCSV && (
            <Button
              variant={testStatus === 'error' ? 'destructive' : 'outline'}
              size="sm"
              onClick={runTest}
              disabled={testStatus === 'testing'}
            >
              {testStatus === 'testing' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : testStatus === 'error' ? (
                'Повторить'
              ) : testStatus === 'success' ? (
                'Повторить'
              ) : (
                'Запустить тест'
              )}
            </Button>
          )}
          {isCSV && testStatus === 'success' && (
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
