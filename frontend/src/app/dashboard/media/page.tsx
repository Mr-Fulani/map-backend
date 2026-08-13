'use client';

import { useEffect, useState } from 'react';
import { mediaApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Images, Layers3, Settings2, WandSparkles } from 'lucide-react';

interface MediaProvider {
  provider_id: string;
  display_name: string;
  is_active: boolean;
  is_configured: boolean;
  capabilities: string[];
  allowed_plan_slugs: string[];
}

interface MediaJob {
  id: number;
  product_image: number;
  provider_id: string;
  operations: string[];
  status: string;
  error_message: string;
  created_at: string;
}

const STATUS_LABELS: Record<string, string> = {
  queued: 'В очереди',
  submitted: 'Передано провайдеру',
  processing: 'Обрабатывается',
  succeeded: 'Готово',
  failed: 'Ошибка',
  cancelled: 'Отменено',
};

export default function MediaPage() {
  const [providers, setProviders] = useState<MediaProvider[]>([]);
  const [jobs, setJobs] = useState<MediaJob[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([mediaApi.providers(), mediaApi.jobs()])
      .then(([providerResponse, jobResponse]) => {
        setProviders(providerResponse.data.data ?? []);
        setJobs(jobResponse.data.data ?? []);
      })
      .catch(() => {
        setProviders([]);
        setJobs([]);
      })
      .finally(() => setLoading(false));
  }, []);

  const pending = jobs.filter((job) => ['queued', 'submitted', 'processing'].includes(job.status)).length;
  const failed = jobs.filter((job) => job.status === 'failed').length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Медиа</h1>
        <p className="text-muted-foreground">
          Проверка, обработка и варианты изображений товаров.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard icon={Layers3} title="Всего задач" value={jobs.length} loading={loading} />
        <SummaryCard icon={WandSparkles} title="В обработке" value={pending} loading={loading} />
        <SummaryCard icon={Images} title="Готово" value={jobs.filter((job) => job.status === 'succeeded').length} loading={loading} />
        <SummaryCard icon={Settings2} title="Ошибки" value={failed} loading={loading} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Провайдеры</CardTitle>
          <CardDescription>
            Сервис выбирается по возможностям, настройкам тенанта и доступности тарифа.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? <Skeleton className="h-20 w-full" /> : providers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Провайдеры ещё не подключены. Администратор может добавить любой совместимый адаптер.
            </p>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {providers.map((provider) => (
                <div key={provider.provider_id} className="rounded-lg border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-medium">{provider.display_name}</p>
                    <Badge variant={provider.is_active && provider.is_configured ? 'default' : 'outline'}>
                      {provider.is_configured ? 'Подключён' : 'Не настроен'}
                    </Badge>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {provider.capabilities.join(', ') || 'Возможности не указаны'}
                  </p>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Последние задачи</CardTitle>
          <CardDescription>Оригиналы не изменяются — результат сохраняется отдельным вариантом.</CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? <Skeleton className="h-28 w-full" /> : jobs.length === 0 ? (
            <p className="text-sm text-muted-foreground">Задач обработки пока нет.</p>
          ) : (
            <div className="space-y-2">
              {jobs.slice(0, 20).map((job) => (
                <div key={job.id} className="flex flex-col gap-1 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <p className="text-sm font-medium">Задача #{job.id} · изображение #{job.product_image}</p>
                    <p className="text-xs text-muted-foreground">
                      {job.operations.join(', ')}{job.provider_id ? ` · ${job.provider_id}` : ''}
                    </p>
                  </div>
                  <Badge variant={job.status === 'failed' ? 'destructive' : job.status === 'succeeded' ? 'default' : 'secondary'}>
                    {STATUS_LABELS[job.status] ?? job.status}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function SummaryCard({ icon: Icon, title, value, loading }: {
  icon: typeof Images;
  title: string;
  value: number;
  loading: boolean;
}) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4 sm:p-4">
        <Icon className="h-5 w-5 text-muted-foreground" />
        <div>
          <p className="text-xs text-muted-foreground">{title}</p>
          {loading ? <Skeleton className="mt-1 h-6 w-10" /> : <p className="text-xl font-semibold">{value}</p>}
        </div>
      </CardContent>
    </Card>
  );
}
