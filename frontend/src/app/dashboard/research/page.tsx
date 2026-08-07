'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  ExternalLink,
  Globe2,
  RefreshCw,
  Search,
} from 'lucide-react';
import { webResearchApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

interface ResearchRun {
  id: number;
  product_id: number;
  product_article: string;
  product_name: string;
  status: string;
  trigger: string;
  search_provider: string;
  ai_provider: string;
  ai_model: string;
  coverage_before: { score?: number };
  coverage_after: { score?: number };
  result_count: number;
  claim_count: number;
  error_message: string;
  created_at: string;
  finished_at: string | null;
}

interface PageMeta {
  total: number;
  page: number;
  page_size: number;
  next: string | null;
  prev: string | null;
}

interface ResearchSummary {
  total: number;
  active: number;
  need_review: number;
  failed: number;
}

interface SearchProviderStatus {
  mode: string;
  available: boolean;
  providers: Array<{ provider_id: string; display_name: string; available: boolean }>;
}

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'active', label: 'Выполняются' },
  { value: 'need_review', label: 'Нужна проверка' },
  { value: 'completed', label: 'Проверено' },
  { value: 'no_results', label: 'Без результатов' },
  { value: 'failed', label: 'Ошибки' },
];

const STATUS_LABELS: Record<string, string> = {
  queued: 'В очереди',
  running: 'Выполняется',
  need_review: 'Нужна проверка',
  completed: 'Проверено',
  no_results: 'Ничего не найдено',
  skipped: 'Не потребовалось',
  failed: 'Ошибка',
};

function statusVariant(status: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (status === 'failed') return 'destructive';
  if (status === 'need_review') return 'secondary';
  if (status === 'running' || status === 'queued') return 'default';
  return 'outline';
}

export default function ResearchPage() {
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [meta, setMeta] = useState<PageMeta | null>(null);
  const [summary, setSummary] = useState<ResearchSummary>({
    total: 0,
    active: 0,
    need_review: 0,
    failed: 0,
  });
  const [statusFilter, setStatusFilter] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [providerStatus, setProviderStatus] = useState<SearchProviderStatus | null>(null);

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true);
    else setLoading(true);
    try {
      const params: Record<string, unknown> = { page };
      if (statusFilter) params.status = statusFilter;
      const response = await webResearchApi.list(params);
      setRuns(response.data.data ?? []);
      setMeta(response.data.meta ?? null);
      setSummary(response.data.summary ?? {
        total: 0,
        active: 0,
        need_review: 0,
        failed: 0,
      });
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [page, statusFilter]);

  useEffect(() => setPage(1), [statusFilter]);
  useEffect(() => { load().catch(() => undefined); }, [load]);
  useEffect(() => {
    webResearchApi.providers()
      .then((response) => setProviderStatus(response.data.data ?? null))
      .catch(() => setProviderStatus(null));
  }, []);
  useEffect(() => {
    if (summary.active === 0) return;
    const timer = setInterval(() => load(true).catch(() => undefined), 5000);
    return () => clearInterval(timer);
  }, [load, summary.active]);

  return (
    <div className="min-w-0 space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            <Globe2 className="h-6 w-6" />
            Интернет-исследования
          </h1>
          <p className="mt-1 text-muted-foreground">
            Поиск недостающих данных, OEM-кодов и применяемости автозапчастей.
          </p>
        </div>
        <Button variant="outline" onClick={() => load(true)} disabled={refreshing}>
          <RefreshCw className={`mr-2 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
          Обновить
        </Button>
      </div>

      {providerStatus && (
        <Card className={providerStatus.available ? '' : 'border-destructive/40'}>
          <CardContent className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium">Сервисы поиска</p>
              <p className="text-xs text-muted-foreground">
                MAP автоматически выбирает доступный сервис и переключается при временной ошибке.
              </p>
            </div>
            <div className="flex min-w-0 flex-wrap gap-2">
              {providerStatus.providers.map((provider) => (
                <Badge key={provider.provider_id} variant="secondary" className="max-w-full whitespace-normal break-words leading-tight [overflow-wrap:anywhere]">
                  {provider.display_name}
                </Badge>
              ))}
              {!providerStatus.available && (
                <Badge variant="destructive">Временно недоступно</Badge>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <SummaryCard icon={Search} label="Всего запусков" value={summary.total} loading={loading} />
        <SummaryCard icon={Clock3} label="Сейчас выполняются" value={summary.active} loading={loading} />
        <SummaryCard icon={CheckCircle2} label="Ждут проверки" value={summary.need_review} loading={loading} />
        <SummaryCard icon={AlertTriangle} label="Ошибки" value={summary.failed} loading={loading} />
      </div>

      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((filter) => (
          <Button
            key={filter.value}
            size="sm"
            variant={statusFilter === filter.value ? 'default' : 'outline'}
            onClick={() => setStatusFilter(filter.value)}
          >
            {filter.label}
          </Button>
        ))}
      </div>

      <div className="grid gap-3 md:hidden">
        {loading ? <ResearchSkeleton count={5} /> : runs.length === 0 ? (
          <EmptyState />
        ) : runs.map((run) => (
          <Card key={run.id} className="min-w-0 overflow-hidden">
            <CardContent className="min-w-0 space-y-3 p-4">
              <div className="flex min-w-0 flex-col items-start gap-2 min-[420px]:flex-row min-[420px]:justify-between">
                <div className="min-w-0 max-w-full">
                  <p className="break-words font-medium [overflow-wrap:anywhere]">{run.product_name}</p>
                  <p className="break-all font-mono text-xs text-muted-foreground">{run.product_article}</p>
                </div>
                <Badge variant={statusVariant(run.status)} className="max-w-full shrink-0 whitespace-normal leading-tight min-[420px]:max-w-[45%] min-[420px]:text-right">
                  {STATUS_LABELS[run.status] ?? run.status}
                </Badge>
              </div>
              <RunDetails run={run} />
              <Link href={`/dashboard/products/${run.product_id}`} className="block min-w-0">
                <Button className="w-full" variant="outline" size="sm">
                  Открыть товар <ExternalLink className="ml-2 h-3.5 w-3.5" />
                </Button>
              </Link>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="hidden overflow-x-auto rounded-lg border md:block">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Товар</th>
              <th className="px-4 py-3 font-medium">Статус</th>
              <th className="px-4 py-3 font-medium">Результат</th>
              <th className="px-4 py-3 font-medium">Источник</th>
              <th className="px-4 py-3 font-medium">Запущено</th>
              <th className="w-12 px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {loading ? Array.from({ length: 7 }).map((_, index) => (
              <tr key={index} className="border-b">
                {Array.from({ length: 6 }).map((__, column) => (
                  <td key={column} className="px-4 py-3"><Skeleton className="h-5 w-full" /></td>
                ))}
              </tr>
            )) : runs.length === 0 ? (
              <tr><td colSpan={6} className="p-0"><EmptyState /></td></tr>
            ) : runs.map((run) => (
              <tr key={run.id} className="border-b transition-colors hover:bg-muted/30">
                <td className="max-w-[320px] px-4 py-3">
                  <p className="truncate font-medium">{run.product_name}</p>
                  <p className="font-mono text-xs text-muted-foreground">{run.product_article}</p>
                  {run.error_message && (
                    <p className="mt-1 line-clamp-1 text-xs text-destructive">{run.error_message}</p>
                  )}
                </td>
                <td className="px-4 py-3">
                  <Badge variant={statusVariant(run.status)}>
                    {STATUS_LABELS[run.status] ?? run.status}
                  </Badge>
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                  {run.result_count} страниц · {run.claim_count} фактов
                </td>
                <td className="px-4 py-3 text-muted-foreground">
                  {run.search_provider || '—'}
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                  {new Date(run.created_at).toLocaleString('ru-RU')}
                </td>
                <td className="px-4 py-3">
                  <Link href={`/dashboard/products/${run.product_id}`}>
                    <Button variant="ghost" size="icon" title="Открыть товар">
                      <ExternalLink className="h-4 w-4" />
                    </Button>
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {meta && meta.total > meta.page_size && (
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            Страница {meta.page} из {Math.ceil(meta.total / meta.page_size)}
          </p>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={!meta.prev} onClick={() => setPage((value) => value - 1)}>
              <ChevronLeft className="mr-1 h-4 w-4" /> Назад
            </Button>
            <Button size="sm" variant="outline" disabled={!meta.next} onClick={() => setPage((value) => value + 1)}>
              Вперёд <ChevronRight className="ml-1 h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function SummaryCard({ icon: Icon, label, value, loading }: {
  icon: typeof Search;
  label: string;
  value: number;
  loading: boolean;
}) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <Icon className="h-5 w-5 text-muted-foreground" />
        <div>
          <p className="text-xs text-muted-foreground">{label}</p>
          {loading ? <Skeleton className="mt-1 h-7 w-12" /> : (
            <p className="text-xl font-semibold">{value.toLocaleString('ru-RU')}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function RunDetails({ run }: { run: ResearchRun }) {
  return (
    <div className="min-w-0 space-y-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
      <p className="min-w-0 break-words">{run.result_count} страниц · {run.claim_count} фактов</p>
      <p className="min-w-0 break-words">
        <span className="break-all">{run.search_provider || 'Провайдер не выбран'}</span>
        {' · '}{new Date(run.created_at).toLocaleString('ru-RU')}
      </p>
      {run.error_message && <p className="min-w-0 whitespace-pre-wrap break-words text-destructive">{run.error_message}</p>}
    </div>
  );
}

function ResearchSkeleton({ count }: { count: number }) {
  return Array.from({ length: count }).map((_, index) => (
    <Card key={index}><CardContent className="space-y-3 p-4">
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-9 w-full" />
    </CardContent></Card>
  ));
}

function EmptyState() {
  return (
    <div className="px-4 py-16 text-center text-muted-foreground">
      <Globe2 className="mx-auto mb-3 h-10 w-10 opacity-30" />
      <p className="font-medium text-foreground">Исследований пока нет</p>
      <p className="mt-1 text-sm">
        Интернет-поиск запустится автоматически, если каталогам не хватит данных о товаре.
      </p>
      <Link href="/dashboard/products">
        <Button className="mt-4" variant="outline">Перейти к товарам</Button>
      </Link>
    </div>
  );
}
