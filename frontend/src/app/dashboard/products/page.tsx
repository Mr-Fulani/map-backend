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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
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
  ArrowUp,
  ArrowDown,
  ChevronsUpDown,
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
  sync_excluded: boolean;
  listing_status: string | null;
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

const LISTING_STATUS_LABEL: Record<string, string> = {
  active: 'Активен',
  pending: 'Модерация',
  queued: 'В очереди',
  requires_review: 'На проверке',
  limit_reached: 'Лимит',
  rejected: 'Отклонён',
  draft: 'Черновик',
  archived: 'Архив',
  deleted: 'Удалён',
};

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'outline';

function listingBadgeVariant(status: string | null): BadgeVariant {
  if (!status) return 'secondary';
  if (status === 'active') return 'default';
  if (status === 'rejected' || status === 'deleted') return 'destructive';
  return 'outline';
}

function SortHeader({
  field,
  ordering,
  onSort,
  className,
  children,
}: {
  field: string;
  ordering: string;
  onSort: (next: string) => void;
  className?: string;
  children: React.ReactNode;
}) {
  const isAsc = ordering === field;
  const isDesc = ordering === `-${field}`;
  const icon = isAsc ? <ArrowUp className="h-3 w-3" /> : isDesc ? <ArrowDown className="h-3 w-3" /> : <ChevronsUpDown className="h-3 w-3 opacity-40" />;

  function handleClick() {
    if (!isAsc && !isDesc) onSort(field);
    else if (isAsc) onSort(`-${field}`);
    else onSort('');
  }

  return (
    <th className={className}>
      <button
        type="button"
        onClick={handleClick}
        className="inline-flex items-center gap-1 hover:text-foreground"
      >
        {children}
        {icon}
      </button>
    </th>
  );
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
  const [ordering, setOrdering] = useState('');
  const [listingFilter, setListingFilter] = useState<string>('');
  const [needsReviewFilter, setNeedsReviewFilter] = useState(false);
  const [excludedFilter, setExcludedFilter] = useState(false);
  const [catalogDomainFilter, setCatalogDomainFilter] = useState<string>('');
  const [catalogCategoryFilter, setCatalogCategoryFilter] = useState<string>('');
  const [catalogCategories, setCatalogCategories] = useState<TenantCatalogCategory[]>([]);
  const [categorySearch, setCategorySearch] = useState('');
  const [categoryDropdownOpen, setCategoryDropdownOpen] = useState(false);
  const categorySearchRef = useRef<HTMLDivElement>(null);
  const [catalogDomains, setCatalogDomains] = useState<CatalogDomain[]>([]);
  const [categoryAssignValue, setCategoryAssignValue] = useState<string>('');
  const [categoryAssignLoading, setCategoryAssignLoading] = useState(false);
  const [categoryAssignError, setCategoryAssignError] = useState('');
  const [excludeLoading, setExcludeLoading] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteLoading, setDeleteLoading] = useState(false);
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
      if (ordering) params.ordering = ordering;
      if (listingFilter) params.listing_filter = listingFilter;
      if (needsReviewFilter) params.needs_review = 'true';
      if (excludedFilter) params.sync_excluded = 'true';
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
  }, [page, debouncedSearch, ordering, listingFilter, needsReviewFilter, excludedFilter, catalogDomainFilter, catalogCategoryFilter]);

  const loadCatalogCategories = useCallback(async () => {
    try {
      const res = await productApi.catalogCategories();
      const categories = (res.data.data ?? []) as TenantCatalogCategory[];
      const active = categories.filter((category) => category.is_active);
      active.sort((a, b) => a.name.localeCompare(b.name, 'ru'));
      setCatalogCategories(active);
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
  }, [debouncedSearch, ordering, listingFilter, needsReviewFilter, excludedFilter, catalogDomainFilter, catalogCategoryFilter]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadCatalogCategories();
    loadCatalogDomains();
  }, [loadCatalogCategories, loadCatalogDomains]);

  useEffect(() => {
    setSelectedIds([]);
  }, [page, debouncedSearch, ordering, listingFilter, needsReviewFilter, excludedFilter, catalogDomainFilter, catalogCategoryFilter]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (categorySearchRef.current && !categorySearchRef.current.contains(e.target as Node)) {
        setCategoryDropdownOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const matchingCategories = categorySearch.trim()
    ? catalogCategories
        .filter((c) => c.name.toLowerCase().includes(categorySearch.toLowerCase()))
        .sort((a, b) => {
          const q = categorySearch.toLowerCase();
          const aExact = a.name.toLowerCase() === q;
          const bExact = b.name.toLowerCase() === q;
          if (aExact !== bExact) return aExact ? -1 : 1;
          const aStarts = a.name.toLowerCase().startsWith(q);
          const bStarts = b.name.toLowerCase().startsWith(q);
          if (aStarts !== bStarts) return aStarts ? -1 : 1;
          return 0;
        })
    : [];

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

  const confirmBulkDelete = async () => {
    setDeleteLoading(true);
    try {
      await productApi.bulkDelete(selectedIds);
      setDeleteDialogOpen(false);
      setSelectedIds([]);
      await load();
    } finally {
      setDeleteLoading(false);
    }
  };

  const excludeFromSync = async (exclude: boolean) => {
    if (selectedIds.length === 0) return;
    setExcludeLoading(true);
    try {
      await productApi.excludeFromSync(selectedIds, exclude);
      setSelectedIds([]);
      await load();
    } finally {
      setExcludeLoading(false);
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
    <>
    <div className="space-y-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight">Каталог товаров</h1>
        <p className="text-muted-foreground">
          {meta ? `${meta.total.toLocaleString('ru-RU')} товаров` : 'Загрузка...'}
        </p>
      </div>

      {/* Фильтры */}
      <div className="flex flex-col gap-3 xl:flex-row">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Артикул, название, бренд..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          {[
            { value: '', label: 'Все' },
            { value: 'listed', label: 'Залистен' },
            { value: 'not_listed', label: 'Не залистен' },
            { value: 'active', label: 'Активен' },
          ].map((f) => (
            <Button
              key={f.value}
              size="sm"
              variant={listingFilter === f.value ? 'default' : 'outline'}
              onClick={() => setListingFilter(listingFilter === f.value && f.value !== '' ? '' : f.value)}
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
          <Button
            size="sm"
            variant={excludedFilter ? 'default' : 'outline'}
            onClick={() => setExcludedFilter((value) => !value)}
          >
            Исключён
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

      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <span className="text-xs font-medium text-muted-foreground">
          {tenant?.name ? `Категория ${tenant.name}` : 'Категория каталога'}
        </span>
        <div className="flex w-full min-w-0 gap-1 sm:w-auto">
          <div className="relative min-w-0 flex-1 sm:flex-none" ref={categorySearchRef}>
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="h-9 w-full pl-7 text-sm sm:w-44"
              placeholder="Поиск категории..."
              value={categorySearch}
              onChange={(e) => {
                setCategorySearch(e.target.value);
                setCategoryDropdownOpen(true);
                if (!e.target.value.trim()) setCatalogCategoryFilter('');
              }}
              onFocus={() => categorySearch && setCategoryDropdownOpen(true)}
            />
            {categoryDropdownOpen && matchingCategories.length > 0 && (
              <div className="absolute left-0 top-full z-20 mt-1 max-h-52 w-full min-w-[200px] overflow-y-auto rounded-md border bg-background shadow-md">
                {matchingCategories.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className="w-full px-3 py-2 text-left text-sm hover:bg-accent"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => {
                      setCatalogCategoryFilter(String(c.id));
                      setCategorySearch(c.name);
                      setCategoryDropdownOpen(false);
                    }}
                  >
                    {c.name}
                  </button>
                ))}
              </div>
            )}
          </div>
          <select
            value={catalogCategoryFilter}
            onChange={(event) => {
              setCatalogCategoryFilter(event.target.value);
              setCategorySearch('');
            }}
            className="h-9 min-w-0 flex-1 rounded-md border border-input bg-background px-3 text-sm"
          >
            <option value="">Все категории</option>
            {catalogCategories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </div>
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
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
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
                  onClick={() => excludeFromSync(true)}
                  disabled={excludeLoading}
                  className="text-destructive focus:text-destructive"
                >
                  Исключить из синхронизации
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => excludeFromSync(false)}
                  disabled={excludeLoading}
                >
                  Восстановить синхронизацию
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => setDeleteDialogOpen(true)}
                  className="text-destructive focus:text-destructive"
                >
                  Удалить безвозвратно
                </DropdownMenuItem>
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
      <div className="grid gap-3 lg:hidden">
        {loading
          ? Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="rounded-lg border p-3">
                <div className="flex gap-3">
                  <Skeleton className="h-14 w-14 shrink-0 rounded-md" />
                  <div className="min-w-0 flex-1 space-y-2">
                    <Skeleton className="h-4 w-28" />
                    <Skeleton className="h-4 w-full" />
                    <Skeleton className="h-4 w-2/3" />
                  </div>
                </div>
              </div>
            ))
          : products.length === 0
            ? (
              <div className="rounded-lg border px-4 py-12 text-center text-muted-foreground">
                <Package className="mx-auto mb-3 h-10 w-10 opacity-30" />
                {search ? 'Ничего не найдено' : 'Товаров пока нет'}
              </div>
            )
            : products.map((p) => (
              <div key={p.id} className="rounded-lg border bg-card p-3">
                <div className="flex items-start gap-3">
                  <input
                    type="checkbox"
                    aria-label={`Выбрать товар ${p.article}`}
                    checked={selectedIds.includes(p.id)}
                    onChange={() => toggleProduct(p.id)}
                    className="mt-5 h-4 w-4 shrink-0 rounded border-muted-foreground"
                  />
                  <Link
                    href={`/dashboard/products/${p.id}?returnTo=${encodeURIComponent(currentListHref)}`}
                    className="shrink-0"
                  >
                    <div className="relative h-14 w-14 overflow-hidden rounded-md border bg-muted">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={p.primary_thumb_url || getCategoryPlaceholder(p.category_1c ?? '', p.name)}
                        alt=""
                        className="h-full w-full object-cover"
                      />
                      {p.images_count > 0 && (
                        <span className="absolute bottom-0 right-0 rounded-tl bg-black/60 px-1 py-0.5 text-[10px] leading-none text-white">
                          {p.images_count}
                        </span>
                      )}
                    </div>
                  </Link>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <Link
                        href={`/dashboard/products/${p.id}?returnTo=${encodeURIComponent(currentListHref)}`}
                        className="min-w-0 break-words text-sm font-medium leading-5 hover:underline"
                      >
                        {p.name}
                      </Link>
                      <div className="flex shrink-0 gap-1">
                        {p.sync_excluded && (
                          <Badge variant="outline" className="border-destructive/50 text-destructive">
                            Исключён
                          </Badge>
                        )}
                        <Badge variant={listingBadgeVariant(p.listing_status)}>
                          {p.listing_status ? LISTING_STATUS_LABEL[p.listing_status] ?? p.listing_status : 'Не залистен'}
                        </Badge>
                      </div>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                      <Link
                        href={`/dashboard/products/${p.id}?returnTo=${encodeURIComponent(currentListHref)}`}
                        className="font-mono font-medium text-primary hover:underline"
                      >
                        {p.article}
                      </Link>
                      {p.brand && <span className="break-words">{p.brand}</span>}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      <Badge
                        variant={p.ai_status === 'ready' ? 'default' : 'outline'}
                        className="gap-1 whitespace-nowrap"
                      >
                        <Sparkles className="h-3 w-3" />
                        AI {p.ai_status === 'ready' ? 'готов' : 'нет'}
                      </Badge>
                      <Badge
                        variant={ENRICHMENT_VARIANTS[p.enrichment_status] ?? 'outline'}
                        className="gap-1 whitespace-nowrap"
                      >
                        <Database className="h-3 w-3" />
                        {ENRICHMENT_LABELS[p.enrichment_status] ?? p.enrichment_status}
                      </Badge>
                      <Badge
                        variant={CATALOG_DOMAIN_VARIANTS[p.catalog_classification?.domain ?? 'unknown'] ?? 'outline'}
                        className="whitespace-nowrap"
                      >
                        {catalogDomainLabel(p.catalog_classification?.domain ?? 'unknown')}
                      </Badge>
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                      <div>
                        <p className="text-xs text-muted-foreground">Цена</p>
                        <p className="font-medium">{Number(p.price).toLocaleString('ru-RU')} ₽</p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Остаток</p>
                        <p className={p.stock_qty === 0 ? 'font-medium text-destructive' : 'font-medium'}>
                          {p.stock_qty}
                        </p>
                      </div>
                    </div>
                    <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                      {p.catalog_category?.name || p.category_1c || 'Без категории'}
                    </p>
                  </div>
                </div>
              </div>
            ))}
      </div>

      <div className="hidden overflow-x-auto rounded-lg border lg:block">
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
              <SortHeader field="ai_status" ordering={ordering} onSort={setOrdering} className="px-4 py-3 font-medium text-center">Готовность</SortHeader>
              <SortHeader field="price" ordering={ordering} onSort={setOrdering} className="px-4 py-3 font-medium text-right">Цена</SortHeader>
              <SortHeader field="stock_qty" ordering={ordering} onSort={setOrdering} className="px-4 py-3 font-medium text-right">Остаток</SortHeader>
              <SortHeader field="listing_status" ordering={ordering} onSort={setOrdering} className="px-4 py-3 font-medium text-center">Листинг</SortHeader>
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
                    className={`border-b transition-colors hover:bg-muted/30 ${p.sync_excluded ? 'opacity-60' : ''}`}
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
                      {p.sync_excluded && (
                        <div className="mt-0.5">
                          <Badge variant="outline" className="border-destructive/50 text-xs text-destructive">
                            Исключён
                          </Badge>
                        </div>
                      )}
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
                      <Badge variant={listingBadgeVariant(p.listing_status)}>
                        {p.listing_status ? LISTING_STATUS_LABEL[p.listing_status] ?? p.listing_status : 'Не залистен'}
                      </Badge>
                    </td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>

      {/* Пагинация */}
      {meta && meta.total > meta.page_size && (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
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
    <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Удалить товары безвозвратно?</DialogTitle>
          <DialogDescription>
            Будет удалено товаров: <strong>{selectedIds.length}</strong>.
            {selectedIds.some((id) => !products.find((p) => p.id === id)?.sync_excluded) && (
              <span className="mt-2 block rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800">
                Часть выбранных товаров не исключена из синхронизации — они могут вернуться при следующем обновлении из 1С/CSV.
              </span>
            )}
            {products.filter((p) => selectedIds.includes(p.id)).every((p) => p.sync_excluded) && (
              <span className="mt-2 block rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800">
                Товары исключены из синхронизации, но если они есть в 1С/CSV — вернутся при следующем обновлении.
              </span>
            )}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => setDeleteDialogOpen(false)} disabled={deleteLoading}>
            Отмена
          </Button>
          <Button variant="destructive" onClick={confirmBulkDelete} disabled={deleteLoading}>
            {deleteLoading ? 'Удаляем...' : 'Удалить'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </>
  );
}
