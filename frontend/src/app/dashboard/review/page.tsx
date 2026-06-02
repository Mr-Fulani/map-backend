'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { Check, ChevronLeft, ChevronRight, RefreshCw, Search, X } from 'lucide-react';
import { productApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useDebounce } from '@/lib/hooks';

type ReviewType = 'all' | 'fitment' | 'fact' | 'classification';
type ReviewAction = 'approve' | 'reject';

interface ReviewProduct {
  id: number;
  article: string;
  name: string;
  brand: string;
  category_1c: string;
}

interface ReviewItem {
  id: string;
  type: Exclude<ReviewType, 'all'>;
  record_id: number;
  product: ReviewProduct;
  title: string;
  reason: string;
  source_id: string;
  confidence: number;
  needs_review: boolean;
  review_status: string;
  updated_at: string;
}

interface Meta {
  total: number;
  page: number;
  page_size: number;
  next: string | null;
  prev: string | null;
}

const TYPE_LABELS: Record<ReviewItem['type'], string> = {
  fitment: 'Применяемость',
  fact: 'Факт',
  classification: 'Классификация',
};

const TYPE_VARIANTS: Record<ReviewItem['type'], 'default' | 'secondary' | 'outline'> = {
  fitment: 'default',
  fact: 'secondary',
  classification: 'outline',
};

const SOURCE_LABELS: Record<string, string> = {
  ai: 'AI',
  manual: 'Оператор',
  rules: 'Автоопределение',
  tachka: 'Tachka',
};

function confidenceLabel(value: number) {
  return `${Math.round(value * 100)}%`;
}

function sourceLabel(value: string) {
  const label = SOURCE_LABELS[value] ?? value;
  return label || 'Автоопределение';
}

function getErrorMessage(error: unknown, fallback = 'Не удалось применить решение') {
  if (
    typeof error === 'object'
    && error !== null
    && 'response' in error
  ) {
    const response = (
      error as {
        response?: {
          status?: number;
          data?: {
            code?: string;
            detail?: string;
            message?: string;
          };
        };
      }
    ).response;
    const message = response?.data?.message || response?.data?.detail || response?.data?.code;
    if (message && response?.status) return `${response.status}: ${message}`;
    if (message) return message;
    if (response?.status) return `Ошибка API: ${response.status}`;
  }
  return fallback;
}

export default function ReviewQueuePage() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [type, setType] = useState<ReviewType>('all');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState('');
  const debouncedSearch = useDebounce(search, 300);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const params: Record<string, unknown> = { page };
      if (type !== 'all') params.type = type;
      if (debouncedSearch) params.search = debouncedSearch;
      const res = await productApi.reviewQueue(params);
      setItems(res.data.data ?? []);
      setMeta(res.data.meta ?? null);
    } catch (err: unknown) {
      setItems([]);
      setMeta(null);
      setError(getErrorMessage(err, 'Не удалось загрузить очередь проверки'));
    } finally {
      setLoading(false);
    }
  }, [page, type, debouncedSearch]);

  useEffect(() => {
    setPage(1);
  }, [type, debouncedSearch]);

  useEffect(() => {
    load();
  }, [load]);

  const review = async (item: ReviewItem, action: ReviewAction) => {
    const key = `${item.id}:${action}`;
    setActionLoading(key);
    setError('');
    try {
      await productApi.reviewQueueAction(item.type, item.record_id, action);
      setItems((current) => current.filter((candidate) => candidate.id !== item.id));
      setMeta((current) => (
        current ? { ...current, total: Math.max(current.total - 1, 0) } : current
      ));
    } catch (err: unknown) {
      setError(getErrorMessage(err));
    } finally {
      setActionLoading(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-normal">Очередь проверки</h1>
          <p className="text-sm text-muted-foreground">
            Спорные данные из источников, которые ждут решения оператора.
          </p>
        </div>
        <Button variant="outline" onClick={load} disabled={loading}>
          <RefreshCw className="mr-2 h-4 w-4" />
          Обновить
        </Button>
      </div>

      <div className="flex flex-col gap-3 border-b pb-4 md:flex-row md:items-center">
        <div className="relative md:w-80">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Артикул, товар или бренд"
            className="pl-9"
          />
        </div>
        <Select value={type} onValueChange={(value) => setType(value as ReviewType)}>
          <SelectTrigger className="md:w-56">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Все типы</SelectItem>
            <SelectItem value="fitment">Применяемость</SelectItem>
            <SelectItem value="fact">Факты</SelectItem>
            <SelectItem value="classification">Классификация</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="overflow-hidden rounded-md border">
        <div className="hidden grid-cols-[118px_minmax(180px,1fr)_minmax(220px,1.3fr)_112px_154px] gap-3 border-b bg-muted/40 px-4 py-3 text-xs font-medium uppercase text-muted-foreground xl:grid">
          <span>Тип</span>
          <span>Товар</span>
          <span>Данные</span>
          <span>Источник</span>
          <span className="text-right">Действие</span>
        </div>

        {loading ? (
          Array.from({ length: 6 }).map((_, index) => (
            <div
              key={index}
              className="grid grid-cols-1 gap-3 border-b px-4 py-4 xl:grid-cols-[118px_minmax(180px,1fr)_minmax(220px,1.3fr)_112px_154px] xl:gap-3"
            >
              <Skeleton className="h-6 w-24" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-6 w-20" />
              <Skeleton className="h-8 w-full" />
            </div>
          ))
        ) : items.length === 0 ? (
          <div className="px-4 py-12 text-center text-sm text-muted-foreground">
            Нет данных на проверке
          </div>
        ) : (
          items.map((item) => (
            <div
              key={item.id}
              className="grid grid-cols-1 gap-3 border-b px-4 py-4 last:border-b-0 xl:grid-cols-[118px_minmax(180px,1fr)_minmax(220px,1.3fr)_112px_154px] xl:items-start xl:gap-3"
            >
              <div className="flex items-center justify-between gap-3 xl:block">
                <Badge variant={TYPE_VARIANTS[item.type]}>{TYPE_LABELS[item.type]}</Badge>
                <span className="text-xs text-muted-foreground xl:hidden">
                  {sourceLabel(item.source_id)}
                </span>
              </div>
              <div className="min-w-0">
                <Link
                  href={`/dashboard/products/${item.product.id}`}
                  className="block break-words text-sm font-medium leading-5 hover:underline"
                >
                  {item.product.brand ? `${item.product.brand} ` : ''}
                  {item.product.article}
                </Link>
                <div className="mt-1 break-words text-sm leading-5 text-muted-foreground xl:line-clamp-2">
                  {item.product.name}
                </div>
              </div>
              <div className="min-w-0">
                <div className="break-words text-sm font-medium leading-5 xl:line-clamp-2">
                  {item.title}
                </div>
                {item.reason && (
                  <div className="mt-1 break-words text-xs leading-5 text-muted-foreground xl:line-clamp-2">
                    {item.reason}
                  </div>
                )}
                <div className="mt-2 text-xs text-muted-foreground">
                  Уверенность: {confidenceLabel(item.confidence)}
                </div>
              </div>
              <div className="hidden break-words text-sm text-muted-foreground xl:block">
                {sourceLabel(item.source_id)}
              </div>
              <div className="grid grid-cols-2 gap-2 xl:grid-cols-1">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => review(item, 'reject')}
                  disabled={actionLoading !== null}
                  className="w-full"
                >
                  <X className="mr-1 h-4 w-4" />
                  Отклонить
                </Button>
                <Button
                  size="sm"
                  onClick={() => review(item, 'approve')}
                  disabled={actionLoading !== null}
                  className="w-full"
                >
                  <Check className="mr-1 h-4 w-4" />
                  Одобрить
                </Button>
              </div>
            </div>
          ))
        )}
      </div>

      {meta && meta.total > 0 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            Всего: {meta.total}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!meta.prev || loading}
              onClick={() => setPage((current) => Math.max(current - 1, 1))}
            >
              <ChevronLeft className="mr-1 h-4 w-4" />
              Назад
            </Button>
            <span>Страница {meta.page}</span>
            <Button
              variant="outline"
              size="sm"
              disabled={!meta.next || loading}
              onClick={() => setPage((current) => current + 1)}
            >
              Вперёд
              <ChevronRight className="ml-1 h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
