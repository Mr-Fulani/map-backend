'use client';

import { useEffect, useState, useRef, useCallback } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import axios from 'axios';
import { productApi, tenantApi } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Search,
  Package,
  ChevronLeft,
  ChevronRight,
  MoreHorizontal,
  RefreshCw,
  Sparkles,
  Database,
  Settings,
} from 'lucide-react';
import { getCategoryPlaceholder } from '@/lib/category-placeholder';
import { useDebounce } from '@/lib/hooks';

interface Product {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  category_1c: string | null;
  catalog_category: {
    id: number;
    name: string;
    domain: string;
    is_active: boolean;
  } | null;
  price: string;
  stock_qty: number;
  export_enabled: boolean;
  sync_at: string | null;
  images_count: number;
  primary_thumb_url: string;
  ai_status: string;
  enrichment_status: string;
  enrichment_summary: {
    attributes_count: number;
    cross_codes_count: number;
    fitments_count: number;
    latest_parse_status: string;
    latest_parse_at: string | null;
  };
  catalog_classification: {
    domain: string;
    confidence: number;
    reason: string;
    needs_review: boolean;
  } | null;
}

interface TenantCatalogCategory {
  id: number;
  name: string;
  domain: string;
  is_active: boolean;
}

interface CatalogDomain {
  slug: string;
  name: string;
  short_name: string;
  is_active: boolean;
  is_enabled_for_tenant: boolean;
}

interface Meta {
  total: number;
  page: number;
  page_size: number;
  next: string | null;
  prev: string | null;
  domain_counts?: Record<string, number>;
}

interface BulkActionJob {
  id: number;
  action: string;
  status: string;
  total_count: number;
  queued_count: number;
  processed_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  batch_size: number;
  pause_seconds: number;
  next_batch_at: string | null;
}

const unwrapBulkActionJob = (payload: { data?: BulkActionJob } & Partial<BulkActionJob>): BulkActionJob => {
  return payload.data ?? (payload as BulkActionJob);
};

const ENRICHMENT_LABELS: Record<string, string> = {
  ready: 'Обогащён',
  missing: 'Нет данных',
  pending: 'В очереди',
  running: 'В работе',
  success: 'Обогащён',
  need_review: 'Частично',
  not_found: 'Не найден',
  failed: 'Ошибка',
};

const ENRICHMENT_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  ready: 'default',
  success: 'default',
  need_review: 'secondary',
  pending: 'secondary',
  running: 'secondary',
  missing: 'outline',
  not_found: 'outline',
  failed: 'destructive',
};

const CATALOG_DOMAIN_LABELS: Record<string, string> = {
  auto_parts: 'Авто',
  jewellery: 'Украш.',
  apparel: 'Одежда',
  generic: 'Разное',
  unknown: 'Не ясно',
};

const CATALOG_DOMAIN_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  auto_parts: 'default',
  jewellery: 'secondary',
  apparel: 'secondary',
  generic: 'outline',
  unknown: 'outline',
};

function enrichmentTitle(product: Product) {
  const summary = product.enrichment_summary;
  const parts = [
    `Характеристики: ${summary.attributes_count}`,
    `OEM/Cross: ${summary.cross_codes_count}`,
    `Применяемость: ${summary.fitments_count}`,
  ];
  if (summary.latest_parse_status) {
    parts.push(`Последний парсинг: ${summary.latest_parse_status}`);
  }
  return parts.join(', ');
}

function bulkStatusText(job: BulkActionJob) {
  if (job.status === 'cooling_down' && job.next_batch_at) {
    return `Пауза до следующего batch: ${new Date(job.next_batch_at).toLocaleTimeString('ru-RU')}`;
  }
  if (job.action === 'classify_catalog_domain' && job.status === 'success') {
    return 'Классификация доменов и категорий товаров завершена';
  }
  if (job.status === 'success') return 'Все задачи поставлены в очередь';
  if (job.status === 'failed') return 'Постановка задач завершилась с ошибкой';
  if (job.status === 'cancelled') return 'Массовое действие отменено';
  return 'Постановка задач выполняется';
}

function pageFromSearchParams(searchParams: { get: (name: string) => string | null }) {
  const pageParam = Number(searchParams.get('page'));
  return Number.isInteger(pageParam) && pageParam > 0 ? pageParam : 1;
}

export default function ProductsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { tenant } = useAuth();
  const [products, setProducts] = useState<Product[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [search, setSearch] = useState('');
  const [exportFilter, setExportFilter] = useState<string>('');
  const [needsReviewFilter, setNeedsReviewFilter] = useState(false);
  const [catalogDomainFilter, setCatalogDomainFilter] = useState<string>('');
  const [catalogCategoryFilter, setCatalogCategoryFilter] = useState<string>('');
  const [catalogCategories, setCatalogCategories] = useState<TenantCatalogCategory[]>([]);
  const [catalogDomains, setCatalogDomains] = useState<CatalogDomain[]>([]);
  const [categoryAssignValue, setCategoryAssignValue] = useState<string>('');
  const [categoryAssignLoading, setCategoryAssignLoading] = useState(false);
  const [categoryAssignError, setCategoryAssignError] = useState('');
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [bulkLoading, setBulkLoading] = useState(false);
  const [bulkRefreshing, setBulkRefreshing] = useState(false);
  const [bulkJob, setBulkJob] = useState<BulkActionJob | null>(null);
  const [bulkError, setBulkError] = useState('');
  const [bulkUpdatedAt, setBulkUpdatedAt] = useState<string | null>(null);
  const lastBulkStatusRef = useRef<string | null>(null);
  const didMountFiltersRef = useRef(false);
  const supportsAutoPartsEnrichment = tenant?.catalog_domain
    ? ['auto_parts', 'mixed'].includes(tenant.catalog_domain)
    : true;

  const debouncedSearch = useDebounce(search, 300);
  const page = pageFromSearchParams(searchParams);
  const currentListHref = `/dashboard/products${searchParams.toString() ? `?${searchParams.toString()}` : ''}`;

  const updatePage = useCallback((nextPage: number) => {
    const safePage = Math.max(1, nextPage);
    const params = new URLSearchParams(searchParams.toString());

    if (safePage === 1) {
      params.delete('page');
    } else {
      params.set('page', String(safePage));
    }

    const query = params.toString();
    const href = `/dashboard/products${query ? `?${query}` : ''}`;
    router.replace(href, { scroll: false });
  }, [router, searchParams]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page };
      if (debouncedSearch) params.search = debouncedSearch;
      if (exportFilter) params.export_enabled = exportFilter;
      if (needsReviewFilter) params.needs_review = 'true';
      if (catalogDomainFilter) params.catalog_domain = catalogDomainFilter;
      if (catalogCategoryFilter) params.catalog_category = catalogCategoryFilter;

      const res = await productApi.list(params);
      setProducts(res.data.data);
      setMeta(res.data.meta);
    } catch {
      setProducts([]);
    } finally {
      setLoading(false);
    }
  }, [page, debouncedSearch, exportFilter, needsReviewFilter, catalogDomainFilter, catalogCategoryFilter]);

  const loadCatalogCategories = useCallback(async () => {
    try {
      const res = await productApi.catalogCategories();
      const categories = (res.data.data ?? []) as TenantCatalogCategory[];
      setCatalogCategories(categories.filter((category) => category.is_active));
    } catch {
      setCatalogCategories([]);
    }
  }, []);

  const loadCatalogDomains = useCallback(async () => {
    try {
      const res = await tenantApi.catalogDomains();
      const domains = (res.data.data ?? []) as CatalogDomain[];
      setCatalogDomains(domains.filter((domain) => domain.is_active));
    } catch {
      setCatalogDomains([]);
    }
  }, []);

  useEffect(() => {
    if (!didMountFiltersRef.current) {
      didMountFiltersRef.current = true;
      return;
    }

    updatePage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch, exportFilter, needsReviewFilter, catalogDomainFilter, catalogCategoryFilter]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadCatalogCategories();
    loadCatalogDomains();
  }, [loadCatalogCategories, loadCatalogDomains]);

  useEffect(() => {
    setSelectedIds([]);
  }, [page, debouncedSearch, exportFilter, needsReviewFilter, catalogDomainFilter, catalogCategoryFilter]);

  const selectedOnPage = products.filter((product) => selectedIds.includes(product.id));
  const allOnPageSelected = products.length > 0 && selectedOnPage.length === products.length;
  const bulkProgress = bulkJob && bulkJob.total_count > 0
    ? Math.round((bulkJob.processed_count / bulkJob.total_count) * 100)
    : 0;
  const manageableCatalogDomains = catalogDomains.filter((domain) => !['mixed', 'unknown'].includes(domain.slug));
  const enabledCatalogDomains = manageableCatalogDomains.filter((domain) => domain.is_enabled_for_tenant);
  const disabledCatalogDomains = manageableCatalogDomains.filter((domain) => !domain.is_enabled_for_tenant);
  const domainFilters = [
    { value: '', label: 'Все домены' },
    ...enabledCatalogDomains.map((domain) => ({
      value: domain.slug,
      label: domain.short_name || domain.name,
    })),
  ];

  const catalogDomainLabel = (slug: string) => {
    const domain = catalogDomains.find((item) => item.slug === slug);
    return domain?.short_name || domain?.name || CATALOG_DOMAIN_LABELS[slug] || slug;
  };

  const toggleProduct = (id: number) => {
    setSelectedIds((current) => (
      current.includes(id)
        ? current.filter((selectedId) => selectedId !== id)
        : [...current, id]
    ));
  };

  const togglePageSelection = () => {
    setSelectedIds(allOnPageSelected ? [] : products.map((product) => product.id));
  };

  const refreshBulkJob = useCallback(async (jobId: number) => {
    const res = await productApi.bulkActionStatus(jobId);
    setBulkJob(unwrapBulkActionJob(res.data));
    setBulkUpdatedAt(new Date().toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }));
  }, []);

  const refreshBulkJobManually = async (jobId: number) => {
    setBulkRefreshing(true);
    setBulkError('');
    try {
      await refreshBulkJob(jobId);
    } catch {
      setBulkError('Не удалось обновить статус массового действия.');
    } finally {
      setBulkRefreshing(false);
    }
  };

  const runBulkEnrichment = async (action = 'enrich_selected') => {
    if (selectedIds.length === 0) return;

    setBulkLoading(true);
    setBulkError('');
    try {
      const res = await productApi.bulkAction({
        action,
        product_ids: selectedIds,
        source: 'tachka',
        batch_size: 20,
        pause_seconds: 60,
      });
      setBulkJob(unwrapBulkActionJob(res.data));
      lastBulkStatusRef.current = null;
      setBulkUpdatedAt(new Date().toLocaleTimeString('ru-RU', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }));
      setSelectedIds([]);
    } catch (error) {
      const responseData = axios.isAxiosError(error)
        ? (error.response?.data as { message?: string } | undefined)
        : undefined;
      const message = responseData?.message;
      setBulkError(message || 'Не удалось запустить массовое действие. Попробуйте ещё раз.');
    } finally {
      setBulkLoading(false);
    }
  };

  const assignCatalogCategory = async (categoryId: number | null) => {
    if (selectedIds.length === 0) return;

    setCategoryAssignLoading(true);
    setCategoryAssignError('');
    try {
      await productApi.assignCatalogCategory({
        product_ids: selectedIds,
        catalog_category: categoryId,
      });
      setSelectedIds([]);
      setCategoryAssignValue('');
      await load();
    } catch (error) {
      const responseData = axios.isAxiosError(error)
        ? (error.response?.data as { message?: string } | undefined)
        : undefined;
      setCategoryAssignError(responseData?.message || 'Не удалось назначить категорию выбранным товарам.');
    } finally {
      setCategoryAssignLoading(false);
    }
  };

  useEffect(() => {
    if (!bulkJob?.id || ['success', 'failed', 'cancelled'].includes(bulkJob.status)) {
      return;
    }

    const timer = window.setTimeout(() => {
      refreshBulkJob(bulkJob.id).catch(() => undefined);
    }, 5000);

    return () => window.clearTimeout(timer);
  }, [bulkJob, refreshBulkJob]);

  useEffect(() => {
    if (!bulkJob) return;

    const previousStatus = lastBulkStatusRef.current;
    lastBulkStatusRef.current = bulkJob.status;

    if (bulkJob.status === 'success' && previousStatus !== 'success') {
      load();
    }
  }, [bulkJob, load]);

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
          <Button
            size="sm"
            variant={needsReviewFilter ? 'default' : 'outline'}
            onClick={() => setNeedsReviewFilter((value) => !value)}
          >
            На проверке
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {domainFilters.map((f) => (
          <Button
            key={f.value}
            size="sm"
            variant={catalogDomainFilter === f.value ? 'default' : 'outline'}
            onClick={() => setCatalogDomainFilter(f.value)}
          >
            {f.label}
            {meta?.domain_counts && (
              <span className="ml-1 text-xs opacity-70">
                {meta.domain_counts[f.value || 'all'] ?? 0}
              </span>
            )}
          </Button>
        ))}
      </div>

      {disabledCatalogDomains.length > 0 && (
        <div className="flex flex-col gap-3 rounded-lg border bg-muted/30 p-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">
            В фильтрах показаны только подключённые домены. Ещё можно подключить:{' '}
            {disabledCatalogDomains.map((domain) => domain.short_name || domain.name).join(', ')}.
          </p>
          <Button asChild size="sm" variant="outline" className="shrink-0">
            <Link href="/dashboard/settings#catalog-categories">
              <Settings className="h-4 w-4" />
              Настройки категорий
            </Link>
          </Button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">
          {tenant?.name ? `Категория ${tenant.name}` : 'Категория каталога'}
        </span>
        <select
          value={catalogCategoryFilter}
          onChange={(event) => setCatalogCategoryFilter(event.target.value)}
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
        >
          <option value="">Все категории</option>
          {catalogCategories.map((category) => (
            <option key={category.id} value={category.id}>
              {category.name}
            </option>
          ))}
        </select>
      </div>

      {selectedIds.length > 0 && (
        <div className="flex flex-col gap-3 rounded-lg border bg-muted/30 p-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm font-medium">
              Выбрано товаров: {selectedIds.length}
            </p>
            <p className="text-xs text-muted-foreground">
              Массовые действия запускаются фоном: батч 20 товаров, пауза 60 секунд.
            </p>
            {categoryAssignError && (
              <p className="mt-1 text-xs text-destructive">{categoryAssignError}</p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => setSelectedIds([])}>
              Снять выбор
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" disabled={bulkLoading}>
                  <MoreHorizontal className="h-4 w-4" />
                  Быстрые действия
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-72">
                <DropdownMenuLabel>Массовые действия</DropdownMenuLabel>
                <div className="space-y-2 px-2 py-2">
                  <p className="text-xs font-medium text-muted-foreground">
                    Назначить категорию
                  </p>
                  <select
                    value={categoryAssignValue}
                    onChange={(event) => setCategoryAssignValue(event.target.value)}
                    className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm"
                    disabled={categoryAssignLoading}
                  >
                    <option value="">Выбрать</option>
                    {catalogCategories.map((category) => (
                      <option key={category.id} value={category.id}>
                        {category.name}
                      </option>
                    ))}
                  </select>
                  <Button
                    size="sm"
                    className="w-full"
                    onClick={() => assignCatalogCategory(Number(categoryAssignValue))}
                    disabled={categoryAssignLoading || !categoryAssignValue}
                  >
                    {categoryAssignLoading ? 'Назначаем...' : 'Применить к выбранным'}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="w-full"
                    onClick={() => assignCatalogCategory(null)}
                    disabled={categoryAssignLoading}
                  >
                    Снять категорию
                  </Button>
                </div>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() => runBulkEnrichment('classify_catalog_domain')}
                  disabled={bulkLoading}
                >
                  Определить домен и категории товаров
                </DropdownMenuItem>
                {supportsAutoPartsEnrichment ? (
                  <DropdownMenuItem
                    onClick={() => runBulkEnrichment('enrich_then_generate_description')}
                    disabled={bulkLoading}
                  >
                    Обогатить и сгенерировать описание
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem disabled>
                    Автозапчастное обогащение отключено
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      )}

      {(bulkJob || bulkError) && (
        <div className="rounded-lg border bg-background p-3">
          {bulkError ? (
            <p className="text-sm text-destructive">{bulkError}</p>
          ) : bulkJob && (
            <div className="space-y-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-medium">
                    Массовая постановка задач #{bulkJob.id}: {bulkJob.status}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {bulkStatusText(bulkJob)}. Обработано batch-позиций {bulkJob.processed_count} из {bulkJob.total_count}; поставлено в очередь {bulkJob.queued_count}; пропущено {bulkJob.skipped_count}.
                    {bulkUpdatedAt ? ` Обновлено: ${bulkUpdatedAt}.` : ''}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => refreshBulkJobManually(bulkJob.id)}
                  disabled={bulkRefreshing}
                >
                  <RefreshCw className={bulkRefreshing ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
                  {bulkRefreshing ? 'Обновляем...' : 'Обновить'}
                </Button>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary transition-all"
                  style={{ width: `${bulkProgress}%` }}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {/* Таблица */}
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="w-10 px-4 py-3 font-medium">
                <input
                  type="checkbox"
                  aria-label="Выбрать все товары на странице"
                  checked={allOnPageSelected}
                  onChange={togglePageSelection}
                  className="h-4 w-4 rounded border-muted-foreground"
                />
              </th>
              <th className="px-4 py-3 font-medium">Фото</th>
              <th className="px-4 py-3 font-medium">Артикул</th>
              <th className="px-4 py-3 font-medium">Название</th>
              <th className="hidden px-4 py-3 font-medium md:table-cell">Бренд</th>
              <th className="hidden px-4 py-3 font-medium lg:table-cell">Категория</th>
              <th className="px-4 py-3 font-medium text-center">Готовность</th>
              <th className="px-4 py-3 font-medium text-right">Цена</th>
              <th className="px-4 py-3 font-medium text-right">Остаток</th>
              <th className="px-4 py-3 font-medium text-center">Выгрузка</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-b">
                    {Array.from({ length: 10 }).map((_, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : products.length === 0
                ? (
                  <tr>
                    <td colSpan={10} className="px-4 py-16 text-center text-muted-foreground">
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
                    <td className="px-4 py-3">
                      <input
                        type="checkbox"
                        aria-label={`Выбрать товар ${p.article}`}
                        checked={selectedIds.includes(p.id)}
                        onChange={() => toggleProduct(p.id)}
                        className="h-4 w-4 rounded border-muted-foreground"
                      />
                    </td>
                    <td className="px-4 py-2">
                      <Link
                        href={`/dashboard/products/${p.id}?returnTo=${encodeURIComponent(currentListHref)}`}
                        className="block"
                      >
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
                        href={`/dashboard/products/${p.id}?returnTo=${encodeURIComponent(currentListHref)}`}
                        className="font-mono text-xs font-medium text-primary hover:underline"
                      >
                        {p.article}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <Link
                        href={`/dashboard/products/${p.id}?returnTo=${encodeURIComponent(currentListHref)}`}
                        className="line-clamp-1 hover:underline"
                      >
                        {p.name}
                      </Link>
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground md:table-cell">
                      {p.brand || '—'}
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground lg:table-cell">
                      <div className="max-w-56 space-y-1">
                        <span className="line-clamp-1 text-foreground">
                          {p.catalog_category?.name || 'Без категории'}
                        </span>
                        {p.category_1c && (
                          <span className="line-clamp-1 text-xs text-muted-foreground">
                            Источник: {p.category_1c}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col items-center gap-1">
                        <Badge
                          variant={p.ai_status === 'ready' ? 'default' : 'outline'}
                          className="gap-1 whitespace-nowrap"
                          title={p.ai_status === 'ready' ? 'AI-описание сгенерировано' : 'AI-описание ещё не сгенерировано'}
                        >
                          <Sparkles className="h-3 w-3" />
                          AI {p.ai_status === 'ready' ? 'готов' : 'нет'}
                        </Badge>
                        <Badge
                          variant={ENRICHMENT_VARIANTS[p.enrichment_status] ?? 'outline'}
                          className="gap-1 whitespace-nowrap"
                          title={enrichmentTitle(p)}
                        >
                          <Database className="h-3 w-3" />
                          {ENRICHMENT_LABELS[p.enrichment_status] ?? p.enrichment_status}
                        </Badge>
                        <Badge
                          variant={CATALOG_DOMAIN_VARIANTS[p.catalog_classification?.domain ?? 'unknown'] ?? 'outline'}
                          className="whitespace-nowrap"
                          title={p.catalog_classification?.reason || 'Домен товара ещё не классифицирован'}
                        >
                          {catalogDomainLabel(p.catalog_classification?.domain ?? 'unknown')}
                        </Badge>
                      </div>
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
              onClick={() => updatePage(meta.page - 1)}
              disabled={!meta.prev}
            >
              <ChevronLeft className="h-4 w-4" />
              Назад
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => updatePage(meta.page + 1)}
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
