'use client';

import { useEffect, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { accountApi, logApi } from '@/lib/api';
import {
  dashboardMarketplaceParam,
  dashboardPageParam,
  dashboardPositiveIdParam,
  dashboardQueryHref,
} from '@/lib/dashboard-query';
import MarketplaceAccountFilter, {
  marketplaceDisplayName,
} from '@/components/marketplaces/MarketplaceAccountFilter';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { ScrollText, ChevronLeft, ChevronRight } from 'lucide-react';

interface SyncLog {
  id: number;
  event_type: string;
  status: string;
  operation: string;
  provider_result: string;
  marketplace: string | null;
  account_id: number | null;
  account_name: string | null;
  message: string;
  created_at: string;
}

interface Meta {
  total: number;
  page: number;
  page_size: number;
  next: string | null;
  prev: string | null;
}

interface Account {
  id: number;
  name: string;
  marketplace: string;
}

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'ok', label: 'Успех' },
  { value: 'error', label: 'Ошибки' },
  { value: 'warn', label: 'Предупреждения' },
];

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive'> = {
  ok: 'default',
  error: 'destructive',
  warn: 'secondary',
};

const OPERATION_FILTERS = [
  { value: '', label: 'Все операции' },
  { value: 'listing_publish', label: 'Публикация' },
  { value: 'listing_update', label: 'Обновление' },
  { value: 'listing_price_update', label: 'Обновление цены' },
  { value: 'listing_unpublish', label: 'Архивирование' },
  { value: 'listing_delete', label: 'Удаление' },
  { value: 'listing_error', label: 'Ошибка листинга' },
  { value: 'rate_limit_hit', label: 'Лимит API' },
];

export default function LogsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const requestedStatus = searchParams.get('status') ?? '';
  const urlStatus = STATUS_FILTERS.some((item) => item.value === requestedStatus)
    ? requestedStatus
    : '';
  const requestedDate = searchParams.get('date') ?? '';
  const urlDate = /^\d{4}-\d{2}-\d{2}$/.test(requestedDate) ? requestedDate : '';
  const urlPage = dashboardPageParam(searchParams.get('page'));
  const marketplace = dashboardMarketplaceParam(searchParams.get('marketplace'));
  const accountId = dashboardPositiveIdParam(searchParams.get('account'));
  const requestedOperation = searchParams.get('operation') ?? '';
  const operation = OPERATION_FILTERS.some((item) => item.value === requestedOperation)
    ? requestedOperation
    : '';
  const statusFilter = urlStatus;
  const date = urlDate;
  const page = urlPage;
  const [logs, setLogs] = useState<SyncLog[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [loading, setLoading] = useState(true);
  const [accounts, setAccounts] = useState<Account[]>([]);

  useEffect(() => {
    accountApi.list()
      .then((response) => setAccounts(response.data.data ?? response.data))
      .catch(() => setAccounts([]));
  }, []);

  useEffect(() => {
    let active = true;
    const params: Record<string, unknown> = { page };
    if (statusFilter) params.status = statusFilter;
    if (date) params.date = date;
    if (marketplace) params.marketplace = marketplace;
    if (accountId) params.account = accountId;
    if (operation) params.operation = operation;

    logApi.list(params)
      .then((response) => {
        if (!active) return;
        // SyncLogListView использует ListAPIView — формат может быть пагинированным.
        const body = response.data;
        if (body.data) {
          setLogs(body.data);
          setMeta(body.meta);
        } else if (Array.isArray(body)) {
          setLogs(body);
          setMeta(null);
        }
      })
      .catch(() => {
        if (active) setLogs([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => { active = false; };
  }, [accountId, date, marketplace, operation, page, statusFilter]);

  return (
    <div className="space-y-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight">Логи синхронизации</h1>
        <p className="text-muted-foreground">
          {meta ? `${meta.total.toLocaleString('ru-RU')} событий` : 'История событий'}
        </p>
      </div>

      {/* Фильтры */}
      <div className="flex flex-col gap-3 sm:flex-row">
        <div className="flex flex-wrap gap-2">
          {STATUS_FILTERS.map((f) => (
            <Button
              key={f.value}
              size="sm"
              variant={statusFilter === f.value ? 'default' : 'outline'}
              onClick={() => {
                if (f.value === statusFilter) return;
                setLoading(true);
                router.replace(dashboardQueryHref(pathname, searchParams.toString(), {
                  status: f.value,
                  page: null,
                }), { scroll: false });
              }}
            >
              {f.label}
            </Button>
          ))}
        </div>
        <Input
          type="date"
          value={date}
          onChange={(e) => {
            setLoading(true);
            router.replace(dashboardQueryHref(pathname, searchParams.toString(), {
              date: e.target.value,
              page: null,
            }), { scroll: false });
          }}
          className="w-full sm:w-auto"
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,32rem)_minmax(12rem,1fr)]">
        <MarketplaceAccountFilter
          marketplace={marketplace}
          accountId={accountId}
          accounts={accounts}
          onChange={(next) => {
            setLoading(true);
            router.replace(dashboardQueryHref(pathname, searchParams.toString(), {
              marketplace: next.marketplace,
              account: next.accountId,
              page: null,
            }), { scroll: false });
          }}
        />
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>Операция</span>
          <select
            value={operation}
            onChange={(event) => {
              setLoading(true);
              router.replace(dashboardQueryHref(pathname, searchParams.toString(), {
                operation: event.target.value,
                page: null,
              }), { scroll: false });
            }}
            className="h-9 w-full rounded-md border bg-background px-3 text-sm text-foreground"
          >
            {OPERATION_FILTERS.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
        </label>
      </div>

      {/* Таблица */}
      <div className="grid gap-3 md:hidden">
        {loading
          ? Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="rounded-lg border p-3">
                <div className="space-y-2">
                  <Skeleton className="h-5 w-24" />
                  <Skeleton className="h-4 w-32" />
                  <Skeleton className="h-4 w-full" />
                </div>
              </div>
            ))
          : logs.length === 0
            ? (
              <div className="rounded-lg border px-4 py-12 text-center text-muted-foreground">
                <ScrollText className="mx-auto mb-3 h-10 w-10 opacity-30" />
                Событий нет
              </div>
            )
            : logs.map((log) => (
              <div key={log.id} className="rounded-lg border bg-card p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <Badge variant={STATUS_VARIANT[log.status] ?? 'secondary'}>
                      {log.provider_result}
                    </Badge>
                    {log.marketplace && (
                      <Badge variant="outline" className="ml-2">
                        {marketplaceDisplayName(log.marketplace)}
                      </Badge>
                    )}
                    <p className="mt-2 break-words font-mono text-xs text-muted-foreground">
                      {log.operation}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {log.account_name || 'Общее событие tenant'}
                    </p>
                  </div>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {new Date(log.created_at).toLocaleDateString('ru-RU')}
                  </span>
                </div>
                <p className="mt-2 break-words text-sm">{log.message || '—'}</p>
              </div>
            ))}
      </div>

      <div className="hidden overflow-x-auto rounded-lg border md:block">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Результат</th>
              <th className="px-4 py-3 font-medium">Маркетплейс</th>
              <th className="px-4 py-3 font-medium">Аккаунт</th>
              <th className="px-4 py-3 font-medium">Операция</th>
              <th className="px-4 py-3 font-medium">Сообщение</th>
              <th className="hidden px-4 py-3 font-medium md:table-cell">Время</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-b">
                    {Array.from({ length: 6 }).map((_, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : logs.length === 0
                ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-16 text-center text-muted-foreground">
                      <ScrollText className="mx-auto mb-3 h-10 w-10 opacity-30" />
                      Событий нет
                    </td>
                  </tr>
                )
                : logs.map((log) => (
                  <tr key={log.id} className="border-b transition-colors hover:bg-muted/30">
                    <td className="px-4 py-3">
                      <Badge variant={STATUS_VARIANT[log.status] ?? 'secondary'}>
                        {log.provider_result}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">
                      {log.marketplace ? (
                        <Badge variant="outline">
                          {marketplaceDisplayName(log.marketplace)}
                        </Badge>
                      ) : '—'}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {log.account_name || '—'}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                      {log.operation}
                    </td>
                    <td className="px-4 py-3">
                      <p className="line-clamp-2">{log.message || '—'}</p>
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground md:table-cell whitespace-nowrap">
                      {new Date(log.created_at).toLocaleString('ru-RU')}
                    </td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>

      {meta && meta.total > meta.page_size && (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">
            Страница {meta.page} из {Math.ceil(meta.total / meta.page_size)}
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setLoading(true);
                router.replace(dashboardQueryHref(pathname, searchParams.toString(), {
                  page: Math.max(1, page - 1),
                }), { scroll: false });
              }}
              disabled={!meta.prev}
            >
              <ChevronLeft className="h-4 w-4" /> Назад
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setLoading(true);
                router.replace(dashboardQueryHref(pathname, searchParams.toString(), {
                  page: page + 1,
                }), { scroll: false });
              }}
              disabled={!meta.next}
            >
              Вперёд <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
