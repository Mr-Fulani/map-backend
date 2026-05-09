'use client';

import { useEffect, useState, useCallback } from 'react';
import { listingApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ListOrdered, ExternalLink, ChevronLeft, ChevronRight } from 'lucide-react';

interface Listing {
  id: number;
  status: string;
  status_display: string;
  product_article: string;
  product_name: string;
  account_name: string;
  title: string;
  price_on_listing: string;
  external_url: string;
  rejection_reason: string;
  retry_count: number;
  published_at: string | null;
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
  { value: 'active', label: 'Активные' },
  { value: 'pending', label: 'Модерация' },
  { value: 'draft', label: 'Черновики' },
  { value: 'rejected', label: 'Отклонены' },
  { value: 'requires_review', label: 'Требуют проверки' },
  { value: 'archived', label: 'Архив' },
];

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  pending: 'secondary',
  draft: 'outline',
  rejected: 'destructive',
  requires_review: 'destructive',
  archived: 'secondary',
  limit_reached: 'destructive',
};

export default function ListingsPage() {
  const [listings, setListings] = useState<Listing[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page };
      if (statusFilter) params.status = statusFilter;
      const res = await listingApi.list(params);
      setListings(res.data.data);
      setMeta(res.data.meta);
    } catch {
      setListings([]);
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter]);

  useEffect(() => { setPage(1); }, [statusFilter]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Листинги</h1>
        <p className="text-muted-foreground">
          {meta ? `${meta.total.toLocaleString('ru-RU')} объявлений` : 'Загрузка...'}
        </p>
      </div>

      {/* Фильтр по статусу */}
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

      {/* Таблица */}
      <div className="rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Статус</th>
              <th className="px-4 py-3 font-medium">Артикул</th>
              <th className="px-4 py-3 font-medium">Название</th>
              <th className="hidden px-4 py-3 font-medium md:table-cell">Аккаунт</th>
              <th className="px-4 py-3 font-medium text-right">Цена</th>
              <th className="hidden px-4 py-3 font-medium lg:table-cell">Опубликован</th>
              <th className="px-4 py-3 font-medium" />
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 6 }).map((_, i) => (
                  <tr key={i} className="border-b">
                    {Array.from({ length: 7 }).map((_, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : listings.length === 0
                ? (
                  <tr>
                    <td colSpan={7} className="px-4 py-16 text-center text-muted-foreground">
                      <ListOrdered className="mx-auto mb-3 h-10 w-10 opacity-30" />
                      Листингов нет
                    </td>
                  </tr>
                )
                : listings.map((l) => (
                  <tr key={l.id} className="border-b transition-colors hover:bg-muted/30">
                    <td className="px-4 py-3">
                      <Badge variant={STATUS_VARIANT[l.status] ?? 'outline'}>
                        {l.status_display}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">{l.product_article}</td>
                    <td className="px-4 py-3">
                      <p className="line-clamp-1">{l.title || l.product_name}</p>
                      {l.rejection_reason && (
                        <p className="mt-0.5 text-xs text-destructive line-clamp-1">
                          {l.rejection_reason}
                        </p>
                      )}
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground md:table-cell">
                      {l.account_name}
                    </td>
                    <td className="px-4 py-3 text-right font-medium">
                      {Number(l.price_on_listing).toLocaleString('ru-RU')} ₽
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground lg:table-cell">
                      {l.published_at
                        ? new Date(l.published_at).toLocaleDateString('ru-RU')
                        : '—'}
                    </td>
                    <td className="px-4 py-3">
                      {l.external_url && (
                        <a
                          href={l.external_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                        >
                          Avito
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
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
