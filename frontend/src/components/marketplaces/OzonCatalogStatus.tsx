'use client';

import { useEffect, useState } from 'react';
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  FolderTree,
  Loader2,
  RefreshCw,
  Search,
} from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { accountApi } from '@/lib/api';
import type {
  OzonCatalogState,
  OzonCatalogTypesPage,
} from '@/lib/marketplace-account-types';
import { ozonCatalogErrorMessage } from '@/lib/ozon-account-presentation';

interface OzonCatalogStatusProps {
  accountId: number;
  accountActive: boolean;
  canManage: boolean;
  connectionEnabled: boolean;
}

function catalogResponse(body: unknown): OzonCatalogState {
  if (body && typeof body === 'object' && 'data' in body) {
    return (body as { data: OzonCatalogState }).data;
  }
  return body as OzonCatalogState;
}

function catalogTypesResponse(body: unknown): OzonCatalogTypesPage {
  if (
    !body
    || typeof body !== 'object'
    || !('status' in body)
    || (body as { status?: unknown }).status !== 'ok'
    || !('data' in body)
    || !Array.isArray((body as { data?: unknown }).data)
    || !('meta' in body)
  ) {
    throw new Error('Invalid local Ozon catalog response');
  }
  return body as OzonCatalogTypesPage;
}

function safeDate(value: string): string {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString('ru-RU')
    : 'неизвестно';
}

export function OzonCatalogStatus({
  accountId,
  accountActive,
  canManage,
  connectionEnabled,
}: OzonCatalogStatusProps) {
  const [catalog, setCatalog] = useState<OzonCatalogState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [catalogTypes, setCatalogTypes] = useState<OzonCatalogTypesPage | null>(null);
  const [typesLoading, setTypesLoading] = useState(false);
  const [typesLoadFailed, setTypesLoadFailed] = useState(false);
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');

  useEffect(() => {
    let active = true;
    accountApi.getOzonCatalog(accountId)
      .then((response) => {
        if (active) setCatalog(catalogResponse(response.data));
      })
      .catch(() => {
        if (active) setLoadFailed(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [accountId]);

  async function refreshTree() {
    if (!canManage || !connectionEnabled || !accountActive) return;
    const confirmed = window.confirm(
      'MAP выполнит один read-only запрос дерева категорий для этого аккаунта Ozon. '
      + 'Товары, цены и остатки изменяться не будут. Продолжить?',
    );
    if (!confirmed) return;

    setRefreshing(true);
    try {
      const response = await accountApi.refreshOzonCatalogTree(accountId);
      setCatalog(catalogResponse(response.data));
      setCatalogTypes(null);
      setBrowserOpen(false);
      setSearch('');
      setAppliedSearch('');
      setLoadFailed(false);
      toast.success('Справочник категорий Ozon обновлён в режиме только чтения.');
    } catch (error: unknown) {
      toast.error(ozonCatalogErrorMessage(error));
    } finally {
      setRefreshing(false);
    }
  }

  async function loadTypes(page: number, query: string) {
    setTypesLoading(true);
    setTypesLoadFailed(false);
    try {
      const response = await accountApi.listOzonCatalogTypes(accountId, {
        search: query || undefined,
        page,
        page_size: 25,
        language: 'DEFAULT',
      });
      setCatalogTypes(catalogTypesResponse(response.data));
    } catch {
      setTypesLoadFailed(true);
    } finally {
      setTypesLoading(false);
    }
  }

  function toggleBrowser() {
    if (browserOpen) {
      setBrowserOpen(false);
      return;
    }
    setBrowserOpen(true);
    if (!catalogTypes) void loadTypes(1, appliedSearch);
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = search.trim();
    setAppliedSearch(query);
    void loadTypes(1, query);
  }

  const tree = catalog?.tree ?? null;
  const refreshDisabled = (
    loading || refreshing || !canManage || !connectionEnabled || !accountActive
  );

  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FolderTree className="h-4 w-4 text-muted-foreground" />
            <p className="text-sm font-medium">Справочник категорий Ozon</p>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            Локальный снимок для этого аккаунта. Обновление выполняется только вручную и
            ничего не меняет в кабинете Ozon.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          className="w-full shrink-0 sm:w-auto"
          disabled={refreshDisabled}
          onClick={refreshTree}
        >
          {refreshing
            ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
            : <RefreshCw className="mr-2 h-3.5 w-3.5" />}
          {refreshing ? 'Обновляем…' : tree ? 'Обновить категории' : 'Загрузить категории'}
        </Button>
      </div>

      {loading ? (
        <div className="mt-3 h-14 animate-pulse rounded bg-muted" />
      ) : loadFailed ? (
        <div className="mt-3 flex items-start gap-2 rounded border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-muted-foreground">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          Не удалось прочитать локальное состояние справочника. Внешний запрос к Ozon не выполнялся.
        </div>
      ) : tree ? (
        <div className="mt-3 space-y-3">
          <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <p className="text-muted-foreground">Узлов дерева</p>
              <p className="mt-0.5 font-medium">{tree.node_count.toLocaleString('ru-RU')}</p>
            </div>
            <div>
              <p className="text-muted-foreground">Активных типов</p>
              <p className="mt-0.5 font-medium">{tree.active_type_count.toLocaleString('ru-RU')}</p>
            </div>
            <div>
              <p className="text-muted-foreground">Схем характеристик</p>
              <p className="mt-0.5 font-medium">
                {(catalog?.attribute_schema_count ?? 0).toLocaleString('ru-RU')}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">Проверено</p>
              <p className="mt-0.5 font-medium">{safeDate(tree.last_checked_at)}</p>
            </div>
            <p className="break-all font-mono text-[11px] text-muted-foreground sm:col-span-2 lg:col-span-4">
              Ревизия {tree.revision.slice(0, 12)} · {tree.language}
            </p>
          </div>

          <Button type="button" size="sm" variant="outline" onClick={toggleBrowser}>
            <FolderTree className="mr-2 h-3.5 w-3.5" />
            {browserOpen ? 'Скрыть категории Ozon' : 'Посмотреть категории Ozon'}
          </Button>

          {browserOpen && (
            <div className="space-y-3 rounded-md border bg-background p-3">
              <div>
                <p className="text-sm font-medium">Категории Ozon этого аккаунта</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Поиск идёт по сохранённому снимку. Он не обращается в Ozon и не меняет
                  Каталог MAP или категории Avito.
                </p>
              </div>
              <form className="flex flex-col gap-2 sm:flex-row" onSubmit={submitSearch}>
                <div className="relative min-w-0 flex-1">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    aria-label="Поиск категории Ozon"
                    className="pl-9"
                    value={search}
                    maxLength={120}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Например: амортизатор или тормозные колодки"
                  />
                </div>
                <Button type="submit" size="sm" disabled={typesLoading}>
                  {typesLoading && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}
                  Найти
                </Button>
              </form>

              {typesLoading && !catalogTypes ? (
                <div className="h-20 animate-pulse rounded bg-muted" />
              ) : typesLoadFailed ? (
                <div className="flex items-start gap-2 rounded border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-muted-foreground">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                  Не удалось прочитать локальный список категорий. Внешний запрос к Ozon не выполнялся.
                </div>
              ) : catalogTypes && catalogTypes.data.length > 0 ? (
                <div className="space-y-2">
                  {catalogTypes.data.map((item) => (
                    <div
                      key={`${item.description_category_id}:${item.type_id}`}
                      className="rounded-md border p-3"
                    >
                      <p className="text-xs text-muted-foreground">{item.category_path}</p>
                      <p className="mt-1 text-sm font-medium">{item.type_name}</p>
                      <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                        Категория Ozon {item.description_category_id} · тип {item.type_id}
                      </p>
                    </div>
                  ))}
                </div>
              ) : catalogTypes ? (
                <p className="rounded-md border border-dashed p-4 text-center text-sm text-muted-foreground">
                  {appliedSearch
                    ? `По запросу «${appliedSearch}» ничего не найдено.`
                    : 'В локальном снимке нет доступных типов товаров.'}
                </p>
              ) : null}

              {catalogTypes && catalogTypes.meta.total > 0 && (
                <div className="flex flex-col gap-2 border-t pt-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
                  <span>
                    Найдено {catalogTypes.meta.total.toLocaleString('ru-RU')} · страница {catalogTypes.meta.page}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!catalogTypes.meta.prev || typesLoading}
                      onClick={() => void loadTypes(catalogTypes.meta.page - 1, appliedSearch)}
                    >
                      <ChevronLeft className="mr-1 h-3.5 w-3.5" />
                      Назад
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!catalogTypes.meta.next || typesLoading}
                      onClick={() => void loadTypes(catalogTypes.meta.page + 1, appliedSearch)}
                    >
                      Далее
                      <ChevronRight className="ml-1 h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <p className="mt-3 text-xs text-muted-foreground">
          Снимка пока нет. Нажмите «Загрузить категории», чтобы выполнить один подтверждённый
          read-only запрос к Ozon.
        </p>
      )}
    </div>
  );
}
