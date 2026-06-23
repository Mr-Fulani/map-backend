/**
 * Шаг 2: Выбор и настройка источника данных.
 * 1С HTTP / 1С XML / CSV загрузка
 */

'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { datasourceApi } from '@/lib/api';
import {
  ArrowLeft,
  ArrowRight,
  Loader2,
  Server,
  FileSpreadsheet,
  FileCode2,
  Upload,
  CheckCircle2,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';

interface StepDatasourceProps {
  data: Record<string, unknown>;
  onNext: (data?: Record<string, unknown>) => void;
  onBack: () => void;
}

const SOURCES = [
  {
    id: '1c_http',
    title: '1С HTTP-сервис',
    description: 'Автоматическая синхронизация через HTTP API вашей 1С',
    icon: Server,
    recommended: true,
  },
  {
    id: '1c_xml',
    title: '1С XML выгрузка',
    description: 'Загрузка XML-файлов, экспортированных из 1С',
    icon: FileCode2,
    recommended: false,
  },
  {
    id: 'csv',
    title: 'CSV / Excel',
    description: 'Ручная загрузка файлов с товарами',
    icon: FileSpreadsheet,
    recommended: false,
  },
];

export function StepDatasource({ data, onNext, onBack }: StepDatasourceProps) {
  const [selectedType, setSelectedType] = useState<string>(
    (data.datasource_type as string) || ''
  );
  const [connectionData, setConnectionData] = useState({
    name: (data.datasource_name as string) || 'Основной склад',
    url: (data.datasource_url as string) || '',
    user: (data.datasource_user as string) || '',
    password: (data.datasource_password as string) || '',
  });
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);

    try {
      if (selectedType === 'csv' && csvFile) {
        // Загружаем CSV
        const { data: uploadResult } = await datasourceApi.uploadCsv(csvFile);
        toast.success(`Файл загружен: ${uploadResult.data?.rows_count || 0} строк`);
        onNext({
          datasource_type: selectedType,
          datasource_name: connectionData.name,
          datasource_id: uploadResult.data?.id,
        });
      } else {
        // Создаём подключение 1С
        const { data: result } = await datasourceApi.create({
          name: connectionData.name,
          type: selectedType,
          url: connectionData.url,
          user: connectionData.user,
          password: connectionData.password,
        });
        toast.success('Источник данных создан!');
        onNext({
          datasource_type: selectedType,
          datasource_name: connectionData.name,
          datasource_id: result.id,
        });
      }
    } catch (err: unknown) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        'Не удалось создать подключение';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Source type selector */}
      <div className="grid gap-3 sm:grid-cols-3">
        {SOURCES.map((source) => (
          <button
            key={source.id}
            type="button"
            onClick={() => setSelectedType(source.id)}
            className={cn(
              'relative flex flex-col items-center gap-2 rounded-xl border p-4 text-center transition-all hover:border-primary/50',
              selectedType === source.id
                ? 'border-primary bg-primary/5 ring-1 ring-primary/20'
                : 'border-border'
            )}
          >
            {source.recommended && (
              <span className="absolute -top-2 right-2 rounded-full bg-primary px-2 py-0.5 text-[10px] font-medium text-primary-foreground">
                Рекомендуем
              </span>
            )}
            <source.icon className={cn(
              'h-8 w-8',
              selectedType === source.id ? 'text-primary' : 'text-muted-foreground'
            )} />
            <span className="text-sm font-medium">{source.title}</span>
            <span className="text-xs text-muted-foreground">{source.description}</span>
            {selectedType === source.id && (
              <CheckCircle2 className="absolute top-2 left-2 h-4 w-4 text-primary" />
            )}
          </button>
        ))}
      </div>

      {/* Dynamic form based on selected type */}
      {(selectedType === '1c_http' || selectedType === '1c_xml') && (
        <div className="space-y-4 rounded-lg border p-4">
          <div className="space-y-2">
            <Label htmlFor="ds-name">Название подключения</Label>
            <Input
              id="ds-name"
              placeholder="Основной склад"
              value={connectionData.name}
              onChange={(e) =>
                setConnectionData((prev) => ({ ...prev, name: e.target.value }))
              }
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="ds-url">URL HTTP-сервиса 1С</Label>
            <Input
              id="ds-url"
              placeholder="https://your-1c.server.ru/avito-sync"
              value={connectionData.url}
              onChange={(e) =>
                setConnectionData((prev) => ({ ...prev, url: e.target.value }))
              }
              required
              className="font-mono text-sm"
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="ds-user">Логин</Label>
              <Input
                id="ds-user"
                placeholder="Admin"
                value={connectionData.user}
                onChange={(e) =>
                  setConnectionData((prev) => ({ ...prev, user: e.target.value }))
                }
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ds-password">Пароль</Label>
              <PasswordInput
                id="ds-password"
                placeholder="••••••••"
                value={connectionData.password}
                onChange={(e) =>
                  setConnectionData((prev) => ({ ...prev, password: e.target.value }))
                }
                required
              />
            </div>
          </div>
        </div>
      )}

      {selectedType === 'csv' && (
        <div className="space-y-4 rounded-lg border p-4">
          <div className="space-y-2">
            <Label htmlFor="ds-csv-name">Название</Label>
            <Input
              id="ds-csv-name"
              placeholder="Прайс-лист"
              value={connectionData.name}
              onChange={(e) =>
                setConnectionData((prev) => ({ ...prev, name: e.target.value }))
              }
              required
            />
          </div>
          <div className="space-y-2">
            <Label>Файл (CSV или Excel)</Label>
            <div className="flex items-center justify-center rounded-lg border-2 border-dashed p-8 transition-colors hover:border-primary/50">
              <label
                htmlFor="csv-upload"
                className="flex cursor-pointer flex-col items-center gap-2 text-center"
              >
                <Upload className="h-8 w-8 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">
                  {csvFile ? csvFile.name : 'Нажмите для выбора файла'}
                </span>
                <span className="text-xs text-muted-foreground/60">
                  .csv, .xlsx • Обязательные колонки: article, name, price, stock_qty
                </span>
                <input
                  id="csv-upload"
                  type="file"
                  accept=".csv,.xlsx,.xls"
                  className="hidden"
                  onChange={(e) => setCsvFile(e.target.files?.[0] || null)}
                />
              </label>
            </div>
          </div>
        </div>
      )}

      <div className="flex justify-between">
        <Button type="button" variant="outline" onClick={onBack}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>
        <Button
          type="submit"
          disabled={
            isLoading ||
            !selectedType ||
            (selectedType === 'csv' && !csvFile) ||
            (selectedType !== 'csv' && !connectionData.url)
          }
        >
          {isLoading ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Сохранение...
            </>
          ) : (
            <>
              Продолжить
              <ArrowRight className="ml-2 h-4 w-4" />
            </>
          )}
        </Button>
      </div>
    </form>
  );
}
