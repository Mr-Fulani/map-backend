'use client';

import { useEffect, useState, useCallback } from 'react';
import { logApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { ScrollText, ChevronLeft, ChevronRight } from 'lucide-react';

interface SyncLog {
  id: number;
  event_type: string;
  status: string;
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

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'success', label: 'Успех' },
  { value: 'error', label: 'Ошибки' },
  { value: 'warning', label: 'Предупреждения' },
];

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive'> = {
  success: 'default',
  error: 'destructive',
  warning: 'secondary',
};

export default function LogsPage() {
  const [logs, setLogs] = useState<SyncLog[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [date, setDate] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page };
      if (statusFilter) params.status = statusFilter;
      if (date) params.date = date;
      const res = await logApi.list(params);
      // SyncLogListView использует ListAPIView — формат может быть пагинированным
      const body = res.data;
      if (body.data) {
        setLogs(body.data);
        setMeta(body.meta);
      } else if (Array.isArray(body)) {
        setLogs(body);
        setMeta(null);
      }
    } catch {
      setLogs([]);
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, date]);

  useEffect(() => { setPage(1); }, [statusFilter, date]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-4">
      <div>
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
              onClick={() => setStatusFilter(f.value)}
            >
              {f.label}
            </Button>
          ))}
        </div>
        <Input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          className="w-auto"
        />
      </div>

      {/* Таблица */}
      <div className="rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Статус</th>
              <th className="px-4 py-3 font-medium">Событие</th>
              <th className="px-4 py-3 font-medium">Сообщение</th>
              <th className="hidden px-4 py-3 font-medium md:table-cell">Время</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-b">
                    {Array.from({ length: 4 }).map((_, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : logs.length === 0
                ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-16 text-center text-muted-foreground">
                      <ScrollText className="mx-auto mb-3 h-10 w-10 opacity-30" />
                      Событий нет
                    </td>
                  </tr>
                )
                : logs.map((log) => (
                  <tr key={log.id} className="border-b transition-colors hover:bg-muted/30">
                    <td className="px-4 py-3">
                      <Badge variant={STATUS_VARIANT[log.status] ?? 'secondary'}>
                        {log.status}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                      {log.event_type}
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
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            Страница {meta.page} из {Math.ceil(meta.total / meta.page_size)}
          </p>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setPage((p) => p - 1)} disabled={!meta.prev}>
              <ChevronLeft className="h-4 w-4" /> Назад
            </Button>
            <Button size="sm" variant="outline" onClick={() => setPage((p) => p + 1)} disabled={!meta.next}>
              Вперёд <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
