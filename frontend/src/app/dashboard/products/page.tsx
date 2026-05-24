'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import { productApi } from '@/lib/api';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { Search, Package, ChevronLeft, ChevronRight } from 'lucide-react';
import { getCategoryPlaceholder } from '@/lib/category-placeholder';
import { useDebounce } from '@/lib/hooks';

interface Product {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  category_1c: string | null;
  price: string;
  stock_qty: number;
  export_enabled: boolean;
  sync_at: string | null;
  images_count: number;
  primary_thumb_url: string;
}

interface Meta {
  total: number;
  page: number;
  page_size: number;
  next: string | null;
  prev: string | null;
}

export default function ProductsPage() {
  const [products, setProducts] = useState<Product[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [search, setSearch] = useState('');
  const [exportFilter, setExportFilter] = useState<string>('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);

  const debouncedSearch = useDebounce(search, 300);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page };
      if (debouncedSearch) params.search = debouncedSearch;
      if (exportFilter) params.export_enabled = exportFilter;

      const res = await productApi.list(params);
      setProducts(res.data.data);
      setMeta(res.data.meta);
    } catch {
      setProducts([]);
    } finally {
      setLoading(false);
    }
  }, [page, debouncedSearch, exportFilter]);

  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, exportFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Каталог товаров</h1>
        <p className="text-muted-foreground">
          {meta ? `${meta.total.toLocaleString('ru-RU')} товаров` : 'Загрузка...'}
        </p>
      </div>

      {/* Фильтры */}
      <div className="flex flex-col gap-3 sm:flex-row">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Артикул, название, бренд..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex gap-2">
          {[
            { value: '', label: 'Все' },
            { value: 'true', label: 'Выгружается' },
            { value: 'false', label: 'Не выгружается' },
          ].map((f) => (
            <Button
              key={f.value}
              size="sm"
              variant={exportFilter === f.value ? 'default' : 'outline'}
              onClick={() => setExportFilter(f.value)}
            >
              {f.label}
            </Button>
          ))}
        </div>
      </div>

      {/* Таблица */}
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Фото</th>
              <th className="px-4 py-3 font-medium">Артикул</th>
              <th className="px-4 py-3 font-medium">Название</th>
              <th className="hidden px-4 py-3 font-medium md:table-cell">Бренд</th>
              <th className="hidden px-4 py-3 font-medium lg:table-cell">Категория</th>
              <th className="px-4 py-3 font-medium text-right">Цена</th>
              <th className="px-4 py-3 font-medium text-right">Остаток</th>
              <th className="px-4 py-3 font-medium text-center">Выгрузка</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-b">
                    {Array.from({ length: 8 }).map((_, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : products.length === 0
                ? (
                  <tr>
                    <td colSpan={8} className="px-4 py-16 text-center text-muted-foreground">
                      <Package className="mx-auto mb-3 h-10 w-10 opacity-30" />
                      {search ? 'Ничего не найдено' : 'Товаров пока нет'}
                    </td>
                  </tr>
                )
                : products.map((p) => (
                  <tr
                    key={p.id}
                    className="border-b transition-colors hover:bg-muted/30"
                  >
                    <td className="px-4 py-2">
                      <Link href={`/dashboard/products/${p.id}`} className="block">
                        <div className="relative w-10 h-10 rounded-md overflow-hidden border bg-muted flex-shrink-0">
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            src={p.primary_thumb_url || getCategoryPlaceholder(p.category_1c ?? '', p.name)}
                            alt=""
                            className="w-full h-full object-cover"
                          />
                          {p.images_count > 0 && (
                            <span className="absolute bottom-0 right-0 bg-black/60 text-white text-[10px] leading-none px-1 py-0.5 rounded-tl">
                              {p.images_count}
                            </span>
                          )}
                        </div>
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <Link
                        href={`/dashboard/products/${p.id}`}
                        className="font-mono text-xs font-medium text-primary hover:underline"
                      >
                        {p.article}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <Link
                        href={`/dashboard/products/${p.id}`}
                        className="line-clamp-1 hover:underline"
                      >
                        {p.name}
                      </Link>
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground md:table-cell">
                      {p.brand || '—'}
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground lg:table-cell">
                      <span className="line-clamp-1">{p.category_1c || '—'}</span>
                    </td>
                    <td className="px-4 py-3 text-right font-medium">
                      {Number(p.price).toLocaleString('ru-RU')} ₽
                    </td>
                    <td className="px-4 py-3 text-right">
                      <span className={p.stock_qty === 0 ? 'text-destructive' : ''}>
                        {p.stock_qty}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <Badge variant={p.export_enabled ? 'default' : 'secondary'}>
                        {p.export_enabled ? 'Да' : 'Нет'}
                      </Badge>
                    </td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>

      {/* Пагинация */}
      {meta && meta.total > meta.page_size && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            Страница {meta.page} из {Math.ceil(meta.total / meta.page_size)}
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPage((p) => p - 1)}
              disabled={!meta.prev}
            >
              <ChevronLeft className="h-4 w-4" />
              Назад
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPage((p) => p + 1)}
              disabled={!meta.next}
            >
              Вперёд
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
