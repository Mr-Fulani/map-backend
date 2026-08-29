'use client';

import { useEffect, useState, useCallback } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { accountApi, listingApi } from '@/lib/api';
import {
  dashboardMarketplaceParam,
  dashboardPageParam,
  dashboardPositiveIdParam,
  dashboardQueryHref,
} from '@/lib/dashboard-query';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Archive,
  ExternalLink,
  Loader2,
  ListOrdered,
  MapPin,
  RefreshCw,
  Send,
  Trash2,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import ListingDrawer from '@/components/listings/ListingDrawer';
import MarketplaceAccountFilter, {
  marketplaceDisplayName,
} from '@/components/marketplaces/MarketplaceAccountFilter';
import { toast } from 'sonner';

interface Listing {
  id: number;
  status: string;
  status_display: string;
  status_explanation: string;
  delivery_stage: string;
  delivery_retry_at: string | null;
  delivery_retry_reason: string;
  provider_submission_started: boolean;
  lifecycle_actions_blocked: boolean;
  can_check_avito_status: boolean;
  can_check_provider_status: boolean;
  can_publish: boolean;
  rejection_ready_to_retry: boolean;
  product_article: string;
  product_name: string;
  account_name: string;
  marketplace: string;
  marketplace_label: string;
  title: string;
  price_on_listing: string;
  external_url: string;
  rejection_reason: string;
  retry_count: number;
  published_at: string | null;
  last_sync_at: string | null;
  remote_status: string | null;
  remote_status_checked_at: string | null;
  next_status_check_at: string | null;
  created_at: string;
}

function avitoCheckedLabel(value: string): string {
  return new Date(value).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function providerCheckedAt(listing: Listing): string | null {
  return listing.remote_status_checked_at;
}

function deliveryRetryLabel(value: string): string {
  return new Date(value).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
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
  marketplace_label: string;
  is_active: boolean;
  provider_capabilities: {
    publication: boolean;
    archive: boolean;
    placement_addresses: boolean;
  };
}

interface PlacementAddress {
  id: number;
  account: number;
  account_name: string;
  name: string;
  seller_address_id: string;
  address: string;
  manager_name: string;
  contact_phone: string;
  is_default: boolean;
}

const STATUS_FILTERS = [
  { value: '', label: 'Все' },
  { value: 'active', label: 'Активные' },
  { value: 'queued', label: 'В очереди' },
  { value: 'pending', label: 'Отправка на площадку' },
  { value: 'draft', label: 'Черновики' },
  { value: 'rejected', label: 'Отклонены' },
  { value: 'requires_review', label: 'Требуют проверки' },
  { value: 'limit_reached', label: 'Лимит достигнут' },
  { value: 'archiving', label: 'Снимаются' },
  { value: 'archived', label: 'Архив' },
];

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  queued: 'secondary',
  pending: 'secondary',
  draft: 'outline',
  rejected: 'destructive',
  requires_review: 'destructive',
  archiving: 'secondary',
  archived: 'secondary',
  limit_reached: 'destructive',
};

export default function ListingsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const requestedStatus = searchParams.get('status') ?? '';
  const urlStatus = STATUS_FILTERS.some((item) => item.value === requestedStatus)
    ? requestedStatus
    : '';
  const urlPage = dashboardPageParam(searchParams.get('page'));
  const marketplace = dashboardMarketplaceParam(searchParams.get('marketplace'));
  const accountId = dashboardPositiveIdParam(searchParams.get('account'));
  const statusFilter = urlStatus;
  const page = urlPage;
  const [listings, setListings] = useState<Listing[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [loading, setLoading] = useState(true);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [bulkAction, setBulkAction] = useState('update_placement');
  const [bulkAccountId, setBulkAccountId] = useState('');
  const [bulkStatus, setBulkStatus] = useState('');
  const [placementAddresses, setPlacementAddresses] = useState<PlacementAddress[]>([]);
  const [bulkPlacementAddressId, setBulkPlacementAddressId] = useState('');
  const [bulkManagerName, setBulkManagerName] = useState('');
  const [bulkContactPhone, setBulkContactPhone] = useState('');
  const [bulkSaving, setBulkSaving] = useState(false);
  const [rowActionId, setRowActionId] = useState<number | null>(null);
  const requestedPanel = searchParams.get('panel') === 'pricing' ? 'pricing' : 'listing';
  const listingParam = Number(searchParams.get('listing'));
  const selectedId = Number.isInteger(listingParam) && listingParam > 0 ? listingParam : null;

  const openDrawer = useCallback((listingId: number) => {
    const next = new URLSearchParams(searchParams.toString());
    next.set('listing', String(listingId));
    next.delete('panel');
    router.replace(`${pathname}?${next}`, { scroll: false });
  }, [pathname, router, searchParams]);

  const closeDrawer = useCallback(() => {
    const next = new URLSearchParams(searchParams.toString());
    next.delete('listing');
    next.delete('panel');
    router.replace(next.size ? `${pathname}?${next}` : pathname, { scroll: false });
  }, [pathname, router, searchParams]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page };
      if (statusFilter) params.status = statusFilter;
      if (marketplace) params.marketplace = marketplace;
      if (accountId) params.account = accountId;
      const res = await listingApi.list(params);
      setListings(res.data.data);
      setMeta(res.data.meta);
    } catch {
      setListings([]);
    } finally {
      setLoading(false);
    }
  }, [accountId, marketplace, page, statusFilter]);

  useEffect(() => {
    let active = true;
    const params: Record<string, unknown> = { page };
    if (statusFilter) params.status = statusFilter;
    if (marketplace) params.marketplace = marketplace;
    if (accountId) params.account = accountId;

    listingApi.list(params)
      .then((response) => {
        if (!active) return;
        setListings(response.data.data);
        setMeta(response.data.meta);
      })
      .catch(() => {
        if (active) setListings([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => { active = false; };
  }, [accountId, marketplace, page, statusFilter]);

  useEffect(() => {
    accountApi.list()
      .then((res) => setAccounts(
        (res.data.data ?? res.data).filter((account: Account) => account.is_active),
      ))
      .catch(() => setAccounts([]));
    accountApi.listPlacementAddresses()
      .then((res) => setPlacementAddresses(res.data.data ?? res.data))
      .catch(() => setPlacementAddresses([]));
  }, []);

  const hasLiveDelivery = listings.some((listing) => (
    ['queued', 'pending', 'archiving'].includes(listing.status)
  ));
  const hasProviderTrackedListings = listings.some((listing) => (
    listing.can_check_provider_status
  ));

  useEffect(() => {
    if (!hasLiveDelivery && !hasProviderTrackedListings) return undefined;
    let active = true;
    const params: Record<string, unknown> = { page };
    if (statusFilter) params.status = statusFilter;
    if (marketplace) params.marketplace = marketplace;
    if (accountId) params.account = accountId;

    const timer = window.setInterval(() => {
      listingApi.list(params)
        .then((response) => {
          if (!active) return;
          setListings(response.data.data);
          setMeta(response.data.meta);
        })
        .catch(() => undefined);
    }, hasLiveDelivery ? 15_000 : 60_000);

    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [
    accountId,
    hasLiveDelivery,
    hasProviderTrackedListings,
    marketplace,
    page,
    statusFilter,
  ]);

  const visiblePlacementAddresses = placementAddresses.filter((address) => (
    bulkAccountId && address.account === Number(bulkAccountId)
  ));
  const selectedBulkAccount = accounts.find((account) => account.id === Number(bulkAccountId));
  const bulkCapabilitySupported = Boolean(selectedBulkAccount && (
    bulkAction === 'publish'
      ? selectedBulkAccount.provider_capabilities.publication
      : bulkAction === 'update_placement'
        ? selectedBulkAccount.provider_capabilities.placement_addresses
        : selectedBulkAccount.provider_capabilities.archive
  ));
  const validBulkPlacementAddressId = visiblePlacementAddresses.some(
    (address) => address.id === Number(bulkPlacementAddressId),
  ) ? bulkPlacementAddressId : '';
  const hasPlacementChange = Boolean(
    validBulkPlacementAddressId || bulkManagerName || bulkContactPhone,
  );
  // Контакты из сохранённых адресов (Настройки → Маркетплейсы) для выпадающих списков.
  const bulkManagerOptions = Array.from(new Set(
    visiblePlacementAddresses.map((a) => a.manager_name).filter(Boolean),
  ));
  const bulkPhoneOptions = Array.from(new Set(
    visiblePlacementAddresses.map((a) => a.contact_phone).filter(Boolean),
  ));

  async function applyBulkAction() {
    if (!selectedBulkAccount || !bulkCapabilitySupported) return;
    const actionLabel: Record<string, string> = {
      publish: 'опубликовать',
      archive: 'снять с публикации',
      delete: 'удалить',
      update_placement: 'обновить локацию',
    };
    const statusScope = bulkStatus
      ? ` со статусом «${STATUS_FILTERS.find((item) => item.value === bulkStatus)?.label ?? bulkStatus}»`
      : ' с любым статусом';
    if (!window.confirm(
      `Массово ${actionLabel[bulkAction]} объявления только в аккаунте `
      + `${selectedBulkAccount.marketplace_label} · ${selectedBulkAccount.name}${statusScope}?`,
    )) {
      return;
    }
    setBulkSaving(true);
    try {
      const payload: Record<string, unknown> = {
        action: bulkAction,
        account_id: selectedBulkAccount.id,
        status: bulkStatus || undefined,
      };
      if (bulkAction === 'update_placement') {
        // Отправляем только заполненные поля — пустое значение означает «не менять»,
        // а не «очистить у всех листингов».
        if (validBulkPlacementAddressId) {
          payload.placement_address = Number(validBulkPlacementAddressId);
        }
        if (bulkManagerName) payload.manager_name_override = bulkManagerName;
        if (bulkContactPhone) payload.contact_phone_override = bulkContactPhone;
      }
      const res = await listingApi.bulkAction(payload);
      const data = res.data.data ?? {};
      await load();
      toast.success(`Готово: ${data.success ?? 0} успешно, ${data.skipped ?? 0} пропущено`);
    } catch (err: unknown) {
      const message = (err as { response?: { data?: { message?: string; detail?: string } } })
        ?.response?.data;
      toast.error(message?.message || message?.detail || 'Не удалось выполнить массовое действие');
    } finally {
      setBulkSaving(false);
    }
  }

  async function runListingAction(
    listing: Listing,
    action: 'publish' | 'archive' | 'delete' | 'checkStatus',
  ) {
    setRowActionId(listing.id);
    try {
      if (action === 'publish') {
        await listingApi.publish(listing.id);
        toast.success('Публикация поставлена в очередь');
      } else if (action === 'archive') {
        await listingApi.archive(listing.id);
        toast.success('Снятие с публикации поставлено в очередь');
      } else if (action === 'delete') {
        await listingApi.delete(listing.id);
        toast.success('Удаление поставлено в очередь');
      } else {
        await listingApi.checkStatus(listing.id);
        toast.success(
          `Запросили актуальный статус у ${marketplaceDisplayName(listing.marketplace)}. `
          + 'Результат обновится автоматически.',
        );
      }
      await load();
    } catch (err: unknown) {
      const message = (err as { response?: { data?: {
        code?: string;
        message?: string;
        detail?: string;
      } } })
        ?.response?.data;
      if (message?.code === 'listing_validation_error') openDrawer(listing.id);
      toast.error(message?.message || message?.detail || 'Не удалось выполнить действие');
    } finally {
      setRowActionId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="min-w-0">
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
        className="max-w-2xl"
      />

      <div className="rounded-lg border p-3">
        <div className="mb-3 flex items-center gap-2">
          <MapPin className="h-4 w-4 text-muted-foreground" />
          <p className="text-sm font-medium">Массовые действия с листингами</p>
        </div>
        <div className="grid gap-2 lg:grid-cols-3">
          <select
            value={bulkAction}
            onChange={(e) => {
              setBulkAction(e.target.value);
              setBulkAccountId('');
              setBulkPlacementAddressId('');
              setBulkManagerName('');
              setBulkContactPhone('');
            }}
            className="h-9 min-w-0 rounded-md border bg-background px-3 text-sm"
          >
            <option value="update_placement">Назначить локацию</option>
            <option value="publish">Опубликовать</option>
            <option value="archive">Архивировать</option>
            <option value="delete">Удалить</option>
          </select>
          <select
            value={bulkAccountId}
            onChange={(e) => {
              setBulkAccountId(e.target.value);
              setBulkPlacementAddressId('');
              setBulkManagerName('');
              setBulkContactPhone('');
            }}
            className="h-9 min-w-0 rounded-md border bg-background px-3 text-sm"
          >
            <option value="">Выберите конкретный аккаунт</option>
            {accounts.map((account) => {
              const supported = bulkAction === 'publish'
                ? account.provider_capabilities.publication
                : bulkAction === 'update_placement'
                  ? account.provider_capabilities.placement_addresses
                  : account.provider_capabilities.archive;
              return (
                <option key={account.id} value={account.id} disabled={!supported}>
                  {account.marketplace_label} · {account.name}
                  {!supported ? ' — операция недоступна' : ''}
                </option>
              );
            })}
          </select>
          <select
            value={bulkStatus}
            onChange={(e) => setBulkStatus(e.target.value)}
            className="h-9 min-w-0 rounded-md border bg-background px-3 text-sm"
          >
            <option value="">Любой статус</option>
            {STATUS_FILTERS.filter((item) => item.value).map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          {bulkAction === 'update_placement' && (
            <>
              <select
                value={validBulkPlacementAddressId}
                onChange={(e) => setBulkPlacementAddressId(e.target.value)}
                className="h-9 min-w-0 rounded-md border bg-background px-3 text-sm lg:col-span-3"
              >
                <option value="">Адрес размещения из справочника</option>
                {visiblePlacementAddresses.map((address) => (
                  <option key={address.id} value={address.id}>
                    {address.account_name} · {address.name}
                    {address.seller_address_id ? ` · ID ${address.seller_address_id}` : ''}
                    {address.address ? ` · ${address.address}` : ''}
                  </option>
                ))}
              </select>
              <select
                value={bulkContactPhone}
                onChange={(e) => setBulkContactPhone(e.target.value)}
                className="h-9 min-w-0 rounded-md border bg-background px-3 text-sm"
              >
                <option value="">Телефон — не менять</option>
                {bulkContactPhone && !bulkPhoneOptions.includes(bulkContactPhone) && (
                  <option value={bulkContactPhone}>{bulkContactPhone}</option>
                )}
                {bulkPhoneOptions.map((phone) => (
                  <option key={phone} value={phone}>{phone}</option>
                ))}
              </select>
              <select
                value={bulkManagerName}
                onChange={(e) => setBulkManagerName(e.target.value)}
                className="h-9 min-w-0 rounded-md border bg-background px-3 text-sm lg:col-span-2"
              >
                <option value="">Контактное лицо — не менять</option>
                {bulkManagerName && !bulkManagerOptions.includes(bulkManagerName) && (
                  <option value={bulkManagerName}>{bulkManagerName}</option>
                )}
                {bulkManagerOptions.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            </>
          )}
          {selectedBulkAccount && (
            <p className="text-xs text-muted-foreground lg:col-span-3">
              Точная область: {selectedBulkAccount.marketplace_label} · {selectedBulkAccount.name}
              {bulkStatus
                ? ` · ${STATUS_FILTERS.find((item) => item.value === bulkStatus)?.label ?? bulkStatus}`
                : ' · любой статус'}
            </p>
          )}
          <Button
            onClick={applyBulkAction}
            disabled={
              bulkSaving
              || !bulkAccountId
              || !bulkCapabilitySupported
              || (bulkAction === 'update_placement' && !hasPlacementChange)
            }
          >
            {bulkSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Выполнить
          </Button>
        </div>
      </div>

      {/* Таблица */}
      <div className="grid gap-3 lg:hidden">
        {loading
          ? Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="rounded-lg border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1 space-y-2">
                    <Skeleton className="h-5 w-24" />
                    <Skeleton className="h-4 w-full" />
                    <Skeleton className="h-4 w-2/3" />
                  </div>
                  <Skeleton className="h-8 w-20" />
                </div>
              </div>
            ))
          : listings.length === 0
            ? (
              <div className="rounded-lg border px-4 py-12 text-center text-muted-foreground">
                <ListOrdered className="mx-auto mb-3 h-10 w-10 opacity-30" />
                Листингов нет
              </div>
            )
            : listings.map((l) => (
              <div
                key={l.id}
                className="rounded-lg border bg-card p-3"
                onClick={() => openDrawer(l.id)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <Badge variant={l.rejection_ready_to_retry ? 'secondary' : (l.delivery_stage === 'delivery_failed' ? 'destructive' : (STATUS_VARIANT[l.status] ?? 'outline'))}>
                      {l.status_display}
                    </Badge>
                    <Badge variant="outline" className="ml-2">
                      {l.marketplace_label}
                    </Badge>
                    {l.status_explanation && (
                      <p className="mt-1 text-xs leading-4 text-muted-foreground">
                        {l.status_explanation}
                      </p>
                    )}
                    <p className="mt-2 break-words text-sm font-medium leading-5">
                      {l.title || l.product_name}
                    </p>
                    {l.rejection_reason && !l.rejection_ready_to_retry && (
                      <p className="mt-1 line-clamp-2 text-xs text-destructive">
                        {l.delivery_stage === 'delivery_failed' ? 'Прошлая попытка: ' : ''}
                        {l.rejection_reason}
                      </p>
                    )}
                    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <span className="font-mono">{l.product_article}</span>
                      {l.account_name && <span className="break-words">{l.account_name}</span>}
                      <span>{Number(l.price_on_listing).toLocaleString('ru-RU')} ₽</span>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {l.published_at
                        ? `Опубликован: ${new Date(l.published_at).toLocaleDateString('ru-RU')}`
                        : `Создан: ${new Date(l.created_at).toLocaleDateString('ru-RU')}`}
                    </p>
                    {providerCheckedAt(l) && (
                      <p className="mt-1 text-xs text-green-700 dark:text-green-400">
                        {l.marketplace_label} проверен: {avitoCheckedLabel(providerCheckedAt(l)!)}
                      </p>
                    )}
                    {l.next_status_check_at && l.can_check_provider_status && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Следующая проверка: {avitoCheckedLabel(l.next_status_check_at)}
                      </p>
                    )}
                    {l.delivery_retry_at && (
                      <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
                        {l.delivery_retry_reason || 'Временная задержка.'}{' '}
                        Следующая попытка: {deliveryRetryLabel(l.delivery_retry_at)}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-col gap-1" onClick={(e) => e.stopPropagation()}>
                    {l.external_url && (
                      <a
                        href={l.external_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                        title={`Открыть на ${l.marketplace_label}`}
                      >
                        <ExternalLink className="h-4 w-4" />
                      </a>
                    )}
                    {l.can_publish && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 w-8 p-0"
                        title={l.rejection_ready_to_retry ? 'Отправить исправленную версию' : (l.delivery_stage === 'delivery_failed' ? 'Исправить и отправить снова' : 'Опубликовать')}
                        onClick={() => runListingAction(l, 'publish')}
                        disabled={rowActionId === l.id}
                      >
                        {rowActionId === l.id
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <Send className="h-4 w-4" />}
                      </Button>
                    )}
                    {['active', 'pending'].includes(l.status) && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 w-8 p-0"
                        title={l.lifecycle_actions_blocked
                          ? `Сначала нужно подтвердить результат предыдущей отправки ${l.marketplace_label}`
                          : 'В архив'}
                        onClick={() => runListingAction(l, 'archive')}
                        disabled={rowActionId === l.id || l.lifecycle_actions_blocked}
                      >
                        {rowActionId === l.id
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <Archive className="h-4 w-4" />}
                      </Button>
                    )}
                    {l.can_check_provider_status && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 w-8 p-0"
                        title={`Проверить статус ${l.marketplace_label}`}
                        onClick={() => runListingAction(l, 'checkStatus')}
                        disabled={rowActionId === l.id}
                      >
                        {rowActionId === l.id
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <RefreshCw className="h-4 w-4" />}
                      </Button>
                    )}
                    {l.status !== 'deleted' && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 w-8 p-0 text-destructive hover:text-destructive"
                        title={l.lifecycle_actions_blocked
                          ? `Сначала нужно подтвердить результат предыдущей отправки ${l.marketplace_label}`
                          : 'Удалить'}
                        onClick={() => runListingAction(l, 'delete')}
                        disabled={rowActionId === l.id || l.lifecycle_actions_blocked}
                      >
                        {rowActionId === l.id
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <Trash2 className="h-4 w-4" />}
                      </Button>
                    )}
                  </div>
                </div>
              </div>
            ))}
      </div>

      <div className="hidden overflow-x-auto rounded-lg border lg:block">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Статус</th>
              <th className="px-4 py-3 font-medium">Артикул</th>
              <th className="px-4 py-3 font-medium">Название</th>
              <th className="hidden px-4 py-3 font-medium md:table-cell">Аккаунт</th>
              <th className="px-4 py-3 font-medium text-right">Цена</th>
              <th className="hidden px-4 py-3 font-medium lg:table-cell">Опубликован</th>
              <th className="px-4 py-3 text-right font-medium">Действия</th>
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
                  <tr
                    key={l.id}
                    className="border-b transition-colors hover:bg-muted/30 cursor-pointer"
                    onClick={() => openDrawer(l.id)}
                  >
                    <td className="px-4 py-3">
                      <Badge variant={l.rejection_ready_to_retry ? 'secondary' : (l.delivery_stage === 'delivery_failed' ? 'destructive' : (STATUS_VARIANT[l.status] ?? 'outline'))}>
                        {l.status_display}
                      </Badge>
                      <Badge variant="outline" className="ml-2">
                        {l.marketplace_label}
                      </Badge>
                      {providerCheckedAt(l) && (
                        <p className="mt-1 max-w-40 text-[11px] leading-4 text-green-700 dark:text-green-400">
                          {l.marketplace_label} проверен: {avitoCheckedLabel(providerCheckedAt(l)!)}
                        </p>
                      )}
                      {l.next_status_check_at && l.can_check_provider_status && (
                        <p className="mt-1 max-w-44 text-[11px] leading-4 text-muted-foreground">
                          Следующая проверка: {avitoCheckedLabel(l.next_status_check_at)}
                        </p>
                      )}
                      {l.delivery_retry_at && (
                        <p className="mt-1 max-w-44 text-[11px] leading-4 text-amber-700 dark:text-amber-400">
                          {l.delivery_retry_reason || 'Временная задержка.'}<br />
                          Повтор: {deliveryRetryLabel(l.delivery_retry_at)}
                        </p>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">{l.product_article}</td>
                    <td className="px-4 py-3">
                      <p className="line-clamp-1">{l.title || l.product_name}</p>
                      {l.status_explanation && (
                        <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                          {l.status_explanation}
                        </p>
                      )}
                      {l.rejection_reason && !l.rejection_ready_to_retry && (
                        <p className="mt-0.5 text-xs text-destructive line-clamp-1">
                          {l.delivery_stage === 'delivery_failed' ? 'Прошлая попытка: ' : ''}
                          {l.rejection_reason}
                        </p>
                      )}
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground md:table-cell">
                      <span className="block">{l.account_name}</span>
                      <span className="text-xs">{l.marketplace_label}</span>
                    </td>
                    <td className="px-4 py-3 text-right font-medium">
                      {Number(l.price_on_listing).toLocaleString('ru-RU')} ₽
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground lg:table-cell">
                      {l.published_at
                        ? new Date(l.published_at).toLocaleDateString('ru-RU')
                        : '—'}
                    </td>
                    <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center justify-end gap-1">
                        {l.external_url && (
                          <a
                            href={l.external_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                            title={`Открыть на ${l.marketplace_label}`}
                          >
                            <ExternalLink className="h-4 w-4" />
                          </a>
                        )}
                        {l.can_publish && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 w-8 p-0"
                            title={l.rejection_ready_to_retry ? 'Отправить исправленную версию' : (l.delivery_stage === 'delivery_failed' ? 'Исправить и отправить снова' : 'Опубликовать')}
                            onClick={() => runListingAction(l, 'publish')}
                            disabled={rowActionId === l.id}
                          >
                            {rowActionId === l.id
                              ? <Loader2 className="h-4 w-4 animate-spin" />
                              : <Send className="h-4 w-4" />}
                          </Button>
                        )}
                        {['active', 'pending'].includes(l.status) && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 w-8 p-0"
                            title={l.lifecycle_actions_blocked
                              ? `Сначала нужно подтвердить результат предыдущей отправки ${l.marketplace_label}`
                              : 'В архив'}
                            onClick={() => runListingAction(l, 'archive')}
                            disabled={rowActionId === l.id || l.lifecycle_actions_blocked}
                          >
                            {rowActionId === l.id
                              ? <Loader2 className="h-4 w-4 animate-spin" />
                              : <Archive className="h-4 w-4" />}
                          </Button>
                        )}
                        {l.can_check_provider_status && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 w-8 p-0"
                            title={`Проверить статус ${l.marketplace_label}`}
                            onClick={() => runListingAction(l, 'checkStatus')}
                            disabled={rowActionId === l.id}
                          >
                            {rowActionId === l.id
                              ? <Loader2 className="h-4 w-4 animate-spin" />
                              : <RefreshCw className="h-4 w-4" />}
                          </Button>
                        )}
                        {l.status !== 'deleted' && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 w-8 p-0 text-destructive hover:text-destructive"
                            title={l.lifecycle_actions_blocked
                              ? `Сначала нужно подтвердить результат предыдущей отправки ${l.marketplace_label}`
                              : 'Удалить'}
                            onClick={() => runListingAction(l, 'delete')}
                            disabled={rowActionId === l.id || l.lifecycle_actions_blocked}
                          >
                            {rowActionId === l.id
                              ? <Loader2 className="h-4 w-4 animate-spin" />
                              : <Trash2 className="h-4 w-4" />}
                          </Button>
                        )}
                      </div>
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

      <ListingDrawer
        listingId={selectedId}
        initialPanel={requestedPanel}
        onClose={closeDrawer}
        onActionDone={load}
      />
    </div>
  );
}
