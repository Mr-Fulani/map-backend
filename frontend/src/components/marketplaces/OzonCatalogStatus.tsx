'use client';

import { useEffect, useState } from 'react';
import { AlertCircle, FolderTree, Loader2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { accountApi } from '@/lib/api';
import type { OzonCatalogState } from '@/lib/marketplace-account-types';
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
      setLoadFailed(false);
      toast.success('Справочник категорий Ozon обновлён в режиме только чтения.');
    } catch (error: unknown) {
      toast.error(ozonCatalogErrorMessage(error));
    } finally {
      setRefreshing(false);
    }
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
        <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
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
      ) : (
        <p className="mt-3 text-xs text-muted-foreground">
          Снимка пока нет. Нажмите «Загрузить категории», чтобы выполнить один подтверждённый
          read-only запрос к Ozon.
        </p>
      )}
    </div>
  );
}
