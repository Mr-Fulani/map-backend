'use client';

import { useCallback, useState, useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { accountApi, listingApi, imageApi, productApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import {
  CheckCircle, RefreshCw, Pencil, Crown, Trash2, Plus,
  ChevronLeft, ChevronRight, Send, BarChart3, FileText,
} from 'lucide-react';
import { getCategoryPlaceholder } from '@/lib/category-placeholder';
import {
  CatalogCategoryPicker,
  CatalogCategoryOption,
} from '@/components/products/catalog-category-picker';
import MarketPricingPanel from '@/components/listings/MarketPricingPanel';

interface ListingImage {
  id: number | null;
  url: string;
  thumb_url: string;
  position: number;
  is_primary: boolean;
}

interface BrandOption {
  name: string;
  source: 'category' | 'avito' | 'current';
}

interface ListingDetail {
  id: number;
  status: string;
  status_display: string;
  product_id: number;
  product_article: string;
  product_name: string;
  product_brand: string;
  account_id: number;
  account_name: string;
  title: string;
  description_ai: string;
  ai_confidence: number | null;
  ai_confidence_display: string;
  price_on_listing: string;
  margin_pct: string | null;
  base_price: string;
  ad_type: string;
  placement_address: number | null;
  address_override: string;
  seller_address_id_override: string;
  manager_name_override: string;
  contact_phone_override: string;
  bulk_address: string;
  bulk_seller_address_id: string;
  bulk_manager_name: string;
  bulk_contact_phone: string;
  rejection_reason: string;
  avito_field_warnings?: string[];
  avito_brand_valid: boolean;
  avito_brand_catalog_synced_at: string | null;
  images: ListingImage[];
  catalog_category: {
    id: number;
    name: string;
    parent_id: number | null;
    parent_name: string | null;
    default_margin_pct?: string;
  } | null;
}

interface Account {
  id: number;
  name: string;
  is_active: boolean;
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

interface Props {
  listingId: number | null;
  initialPanel?: 'listing' | 'pricing';
  onClose: () => void;
  onActionDone: () => void;
}

// Вид объявления Avito (AdType). value — точная строка, которую принимает Avito.
const AD_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: 'Товар приобретен на продажу', label: 'Товар приобретён на продажу — перепродажа (B2B)' },
  { value: 'Товар от производителя', label: 'Товар от производителя' },
];
const DEFAULT_AD_TYPE = 'Товар приобретен на продажу';
// Лимит заголовка в Avito Autoload — 100 символов (отличается от лимита
// ручной загрузки).
const AVITO_TITLE_MAX = 100;

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  pending: 'secondary',
  draft: 'outline',
  rejected: 'destructive',
  requires_review: 'destructive',
  archiving: 'secondary',
  archived: 'secondary',
  limit_reached: 'destructive',
};

function ConfidenceBar({ value, label }: { value: number | null; label: string }) {
  if (value === null) return null;
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? 'bg-green-500' : pct >= 50 ? 'bg-yellow-500' : 'bg-red-500';
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted-foreground">AI-уверенность</span>
        <span className="font-medium">{label}</span>
      </div>
      <div className="h-2 w-full rounded-full bg-secondary overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export default function ListingDrawer({
  ...props
}: Props) {
  return (
    <ListingDrawerContent
      key={`${props.listingId ?? 'closed'}:${props.initialPanel ?? 'listing'}`}
      {...props}
    />
  );
}

function ListingDrawerContent({
  listingId, initialPanel = 'listing', onClose, onActionDone,
}: Props) {
  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [loading, setLoading] = useState(listingId !== null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editingBrand, setEditingBrand] = useState(false);
  const [editTitle, setEditTitle] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [editBrand, setEditBrand] = useState('');
  const [brandOptions, setBrandOptions] = useState<BrandOption[]>([]);
  const [brandOptionsLoading, setBrandOptionsLoading] = useState(false);
  const [brandInputFocused, setBrandInputFocused] = useState(false);
  const [brandCategoryScope, setBrandCategoryScope] = useState<string | null>(null);
  const [avitoBrandCatalogLoaded, setAvitoBrandCatalogLoaded] = useState(false);
  const [avitoBrandCatalogSyncedAt, setAvitoBrandCatalogSyncedAt] = useState<string | null>(null);
  const [avitoBrandCatalogStale, setAvitoBrandCatalogStale] = useState(false);
  const [editAddress, setEditAddress] = useState('');
  const [editSellerAddressId, setEditSellerAddressId] = useState('');
  const [editManagerName, setEditManagerName] = useState('');
  const [editContactPhone, setEditContactPhone] = useState('');
  const [editPlacementAddressId, setEditPlacementAddressId] = useState('');
  const [editAccountId, setEditAccountId] = useState('');
  const [editPrice, setEditPrice] = useState('');
  const [editMarginPct, setEditMarginPct] = useState<string>('');
  const [editAdType, setEditAdType] = useState(DEFAULT_AD_TYPE);
  const [categories, setCategories] = useState<CatalogCategoryOption[]>([]);
  const [editCategoryId, setEditCategoryId] = useState('');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [placementAddresses, setPlacementAddresses] = useState<PlacementAddress[]>([]);
  const [previewImg, setPreviewImg] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [photoLoading, setPhotoLoading] = useState<number | 'upload' | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [activePanel, setActivePanel] = useState<'listing' | 'pricing'>(initialPanel);
  const [marketPriceApplied, setMarketPriceApplied] = useState(false);
  const [pricingRefreshKey, setPricingRefreshKey] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const publishPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const open = listingId !== null;

  const applyListingState = useCallback((data: ListingDetail) => {
    setListing(data);
    setEditTitle(data.title);
    setEditDesc(data.description_ai);
    setEditBrand(data.product_brand || '');
    setEditAddress(data.address_override || '');
    setEditSellerAddressId(data.seller_address_id_override || '');
    setEditManagerName(data.manager_name_override || '');
    setEditContactPhone(data.contact_phone_override || '');
    setEditPlacementAddressId(data.placement_address ? String(data.placement_address) : '');
    setEditAccountId(String(data.account_id));
    setEditPrice(data.price_on_listing);
    // Если у листинга нет своей наценки — подтягиваем дефолт из категории
    setEditMarginPct(
      data.margin_pct ?? data.catalog_category?.default_margin_pct ?? '',
    );
    setEditAdType(data.ad_type || DEFAULT_AD_TYPE);
    // Храним выбранную категорию одним id; путь до корня строится по дереву.
    setEditCategoryId(data.catalog_category ? String(data.catalog_category.id) : '');
  }, []);

  useEffect(() => {
    if (listingId === null) return undefined;
    let cancelled = false;

    const loadDrawer = async () => {
      const auxiliaryData = Promise.all([
        accountApi.list().catch(() => null),
        accountApi.listPlacementAddresses().catch(() => null),
        productApi.catalogCategories({ assignable: true }).catch(() => null),
      ]);
      let data: ListingDetail;
      try {
        const listingResponse = await listingApi.get(listingId);
        if (cancelled) return;

        data = listingResponse.data.data;
        applyListingState(data);

        const primaryIdx = data.images.findIndex((image) => image.is_primary);
        setActiveIdx(primaryIdx >= 0 ? primaryIdx : 0);
      } catch {
        if (!cancelled) onClose();
        return;
      } finally {
        if (!cancelled) setLoading(false);
      }

      const [accountsResponse, addressesResponse, categoriesResponse] = await auxiliaryData;
      if (cancelled) return;

      const loadedAddresses: PlacementAddress[] = addressesResponse
        ? (addressesResponse.data.data ?? addressesResponse.data)
        : [];
      setAccounts(accountsResponse ? (accountsResponse.data.data ?? accountsResponse.data) : []);
      setPlacementAddresses(loadedAddresses);
      setCategories(categoriesResponse ? (categoriesResponse.data.data ?? categoriesResponse.data) : []);
      if (!data.placement_address && !data.address_override && !data.seller_address_id_override) {
        const fallback = loadedAddresses.find(
          (address) => address.account === data.account_id && address.is_default,
        );
        if (fallback) {
          setEditPlacementAddressId((current) => current || String(fallback.id));
        }
      }
    };

    void loadDrawer();
    return () => {
      cancelled = true;
      if (publishPollRef.current) clearInterval(publishPollRef.current);
    };
  }, [applyListingState, listingId, onClose]);

  useEffect(() => {
    if (!editingBrand || !listing) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setBrandOptionsLoading(true);
      productApi.brandOptions(listing.product_id, editBrand)
        .then((res) => {
          if (cancelled) return;
          const data = res.data.data;
          setBrandOptions(data.options ?? []);
          setBrandCategoryScope(data.category_scope ?? null);
          setAvitoBrandCatalogLoaded(Boolean(data.catalog_loaded));
          setAvitoBrandCatalogSyncedAt(data.catalog_synced_at ?? null);
          setAvitoBrandCatalogStale(Boolean(data.catalog_stale));
        })
        .catch(() => {
          if (!cancelled) setBrandOptions([]);
        })
        .finally(() => {
          if (!cancelled) setBrandOptionsLoading(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [editingBrand, listing, editBrand]);

  const visiblePlacementAddresses = placementAddresses.filter((address) => (
    !editAccountId || address.account === Number(editAccountId)
  ));
  const selectedPlacementAddress = placementAddresses.find((address) => (
    address.id === Number(editPlacementAddressId)
  ));
  // Контакты из сохранённых адресов аккаунта (Настройки → Маркетплейсы) для выпадающих списков.
  const managerOptions = Array.from(new Set(
    visiblePlacementAddresses.map((a) => a.manager_name).filter(Boolean),
  ));
  const phoneOptions = Array.from(new Set(
    visiblePlacementAddresses.map((a) => a.contact_phone).filter(Boolean),
  ));

  // Дефолтный адрес аккаунта (is_default) — для авто-подстановки. '' если нет.
  const defaultAddressIdForAccount = (accountId: string) => {
    const found = placementAddresses.find(
      (address) => address.account === Number(accountId) && address.is_default,
    );
    return found ? String(found.id) : '';
  };

  const handleOpenChange = (isOpen: boolean) => {
    if (!isOpen) onClose();
  };

  // --- Действия с листингом ---

  const callAction = async (
    key: string,
    fn: () => Promise<unknown>,
    successMsg: string,
    closeAfter = true,
  ) => {
    setActionLoading(key);
    try {
      await fn();
      toast.success(successMsg);
      onActionDone();
      if (closeAfter) onClose();
    } catch (err: unknown) {
      const code = (err as { response?: { data?: { code?: string } } })?.response?.data?.code;
      if (code === 'quota_exceeded') {
        toast.error('AI-кредиты исчерпаны. Обновите тариф в разделе Биллинг.');
      } else if (code === 'invalid_status') {
        toast.error('Действие недоступно для текущего статуса.');
      } else {
        toast.error('Техническая ошибка. Обратитесь в поддержку.');
      }
    } finally {
      setActionLoading(null);
    }
  };

  const handleApprove = () =>
    callAction('approve', () => listingApi.approve(listing!.id), 'Одобрено, задача публикации поставлена');

  // Текущие правки полей листинга — общий пейлоад для «Сохранить» и «Опубликовать».
  const buildEditPayload = () => ({
    title: editTitle,
    description_ai: editDesc,
    account_id: editAccountId ? Number(editAccountId) : undefined,
    price_on_listing: editMarginPct === '' ? editPrice : undefined,
    margin_pct: editMarginPct !== '' ? editMarginPct : null,
    ad_type: editAdType,
    placement_address: editPlacementAddressId ? Number(editPlacementAddressId) : null,
    address_override: editAddress,
    seller_address_id_override: editSellerAddressId,
    manager_name_override: editManagerName,
    contact_phone_override: editContactPhone,
  });

  // Выбранная категория = самый глубокий выбранный узел; null — снять.
  const selectedCategoryId = () => (editCategoryId ? Number(editCategoryId) : null);

  // Категория хранится у товара, не у листинга — сохраняем отдельным вызовом,
  // только если изменилась. assign также закрепляет ручную классификацию.
  const saveCategoryIfChanged = async () => {
    if (!listing) return;
    const selected = selectedCategoryId();
    const current = listing.catalog_category?.id ?? null;
    if (selected === current) return;
    await productApi.assignCatalogCategory({
      product_ids: [listing.product_id],
      catalog_category: selected,
    });
  };

  // Бренд принадлежит товару, поэтому не включаем его в PATCH листинга.
  // Он загружается вместе с листингом и сохраняется отдельным API товара.
  const saveBrandIfChanged = async () => {
    if (!listing || editBrand.trim() === (listing.product_brand || '').trim()) return;
    const response = await productApi.updateBrand(listing.product_id, editBrand.trim());
    const brand = response.data.data.brand || '';
    setListing((current) => current ? { ...current, product_brand: brand } : current);
    setEditBrand(brand);
  };

  const handleSaveBrand = async () => {
    if (!listing) return;
    setActionLoading('brand');
    try {
      await saveBrandIfChanged();
      const refreshed = await listingApi.get(listing.id);
      applyListingState(refreshed.data.data);
      setEditingBrand(false);
      onActionDone();
      toast.success('Бренд товара сохранён');
    } catch {
      toast.error('Не удалось сохранить бренд');
    } finally {
      setActionLoading(null);
    }
  };

  const handleRefreshBrandCatalog = async () => {
    if (!listing) return;
    setActionLoading('brand-catalog');
    try {
      const response = await listingApi.refreshBrandCatalog(listing.id);
      applyListingState(response.data.data);
      toast.success(response.data.data.avito_brand_valid
        ? 'Справочник обновлён — бренд найден в Avito'
        : 'Справочник обновлён, но бренд по-прежнему не найден');
    } catch {
      toast.error('Не удалось обновить справочник Avito. Используется последняя рабочая версия');
    } finally {
      setActionLoading(null);
    }
  };

  const handlePublish = async () => {
    if (!listing) return;
    setActionLoading('publish');
    try {
      // «Опубликовать» = сначала сохранить текущие правки, потом публиковать,
      // чтобы изменения цены/описания/контактов не терялись.
      await saveCategoryIfChanged();
      const saved = await listingApi.updateContent(listing.id, buildEditPayload());
      applyListingState(saved.data.data);
      setEditing(false);
      await listingApi.publish(listing.id);
    } catch (err: unknown) {
      const code = (err as { response?: { data?: { code?: string } } })?.response?.data?.code;
      toast.error(code === 'invalid_status' ? 'Действие недоступно для текущего статуса.' : 'Техническая ошибка.');
      setActionLoading(null);
      return;
    }
    setActionLoading(null);
    setPublishing(true);
    onActionDone();

    // Поллим статус каждые 3 секунды — максимум 2 минуты
    let attempts = 0;
    const MAX_ATTEMPTS = 40;
    publishPollRef.current = setInterval(async () => {
      attempts++;
      try {
        const res = await listingApi.get(listing.id);
        const updated: ListingDetail = res.data.data;
        if (updated.status !== 'draft') {
          clearInterval(publishPollRef.current!);
          setPublishing(false);
          setListing(updated);
          onActionDone();
          if (updated.status === 'active') {
            toast.success('Объявление опубликовано!');
          } else if (updated.status === 'pending') {
            toast.info('Объявление отправлено на модерацию Avito.');
          } else if (updated.status === 'rejected') {
            toast.error(`Публикация отклонена: ${updated.rejection_reason || 'неизвестная причина'}`);
          } else {
            toast.error(`Статус изменился на: ${updated.status_display}`);
          }
        } else if (attempts >= MAX_ATTEMPTS) {
          clearInterval(publishPollRef.current!);
          setPublishing(false);
          toast.error('Публикация занимает больше времени, чем ожидалось. Проверьте статус позже.');
        }
      } catch {
        if (attempts >= MAX_ATTEMPTS) {
          clearInterval(publishPollRef.current!);
          setPublishing(false);
        }
      }
    }, 3000);
  };

  const handleRegenerate = () =>
    callAction('regenerate', () => listingApi.regenerate(listing!.id), 'Задача генерации поставлена в очередь');

  const handleCheckStatus = () =>
    callAction('checkStatus', () => listingApi.checkStatus(listing!.id), 'Проверка статуса Avito поставлена в очередь', false);

  const handleSaveEdit = async () => {
    if (!listing) return;
    setActionLoading('save');
    try {
      if (listing.status !== 'active') await saveCategoryIfChanged();
      const payload = marketPriceApplied
        ? listing.status === 'active'
          ? { price_on_listing: editPrice }
          : { ...buildEditPayload(), price_on_listing: editPrice, margin_pct: null }
        : buildEditPayload();
      const res = await listingApi.updateContent(listing.id, payload);
      applyListingState(res.data.data);
      setMarketPriceApplied(false);
      setEditing(false);
      setPricingRefreshKey((value) => value + 1);
      onActionDone();
      toast.success('Сохранено. Сравнение цен пересчитано.');
    } catch (err: unknown) {
      const message = (err as { response?: { data?: { message?: string; detail?: string } } })
        ?.response?.data;
      toast.error(message?.message || message?.detail || 'Не удалось сохранить');
    } finally {
      setActionLoading(null);
    }
  };

  // --- Управление фото ---

  const handleUploadPhoto = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!listing) return;
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = '';
    setPhotoLoading('upload');
    try {
      const res = await imageApi.upload(listing.product_id, file);
      const img = res.data.data;
      setListing((prev) => prev ? { ...prev, images: [...prev.images, img] } : prev);
      toast.success('Фото добавлено');
    } catch {
      toast.error('Не удалось загрузить фото');
    } finally {
      setPhotoLoading(null);
    }
  };

  const handleDeletePhoto = async (imageId: number) => {
    if (!listing) return;
    setPhotoLoading(imageId);
    try {
      await imageApi.delete(listing.product_id, imageId);
      setListing((prev) => {
        if (!prev) return prev;
        const imgs = prev.images.filter((i) => i.id !== imageId);
        setActiveIdx((idx) => Math.min(idx, Math.max(0, imgs.length - 1)));
        return { ...prev, images: imgs };
      });
    } catch {
      toast.error('Не удалось удалить фото');
    } finally {
      setPhotoLoading(null);
    }
  };

  const handleSetPrimary = async (imageId: number) => {
    if (!listing) return;
    setPhotoLoading(imageId);
    try {
      await imageApi.setPrimary(listing.product_id, imageId);
      setListing((prev) => prev ? {
        ...prev,
        images: prev.images.map((i) => ({ ...i, is_primary: i.id === imageId })),
      } : prev);
    } catch {
      toast.error('Не удалось установить главное фото');
    } finally {
      setPhotoLoading(null);
    }
  };

  const activeImage = listing?.images[activeIdx] ?? null;

  const applyMarketPrice = (price: string) => {
    if (!listing) return;
    setEditPrice(price);
    const base = Number(listing.base_price);
    const nextPrice = Number(price);
    setEditMarginPct(
      base > 0 && Number.isFinite(nextPrice)
        ? (((nextPrice / base) - 1) * 100).toFixed(2)
        : '',
    );
    setMarketPriceApplied(true);
    setActivePanel('listing');
  };

  const isReview = listing?.status === 'requires_review';
  const isDraft = listing?.status === 'draft';
  const isRejected = listing?.status === 'rejected';
  const isArchived = listing?.status === 'archived';
  const isPending = listing?.status === 'pending';
  const isLimitReached = listing?.status === 'limit_reached';
  const canPublish = isDraft || isRejected || isArchived || isLimitReached;
  const canRegenerate = isReview || isDraft || isRejected;
  const busy = actionLoading !== null;

  return (
    <>
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent
          side="right"
          className="h-[100dvh] w-[100dvw] min-w-0 max-w-[100dvw] overflow-hidden p-0 sm:max-w-[100dvw] xl:w-[min(96vw,1440px)] xl:max-w-[min(96vw,1440px)]"
        >
          {loading || !listing ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              Загрузка...
            </div>
          ) : (
            <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
              <div className="min-w-0 shrink-0 overflow-hidden border-b bg-background/95 px-4 pb-3 pt-[max(0.75rem,env(safe-area-inset-top))] backdrop-blur xl:hidden">
                <p className="truncate pr-10 text-xs text-muted-foreground">{listing.product_article}</p>
                <p className="truncate pr-10 text-sm font-medium">{listing.product_name}</p>
                <div className="mt-3 grid min-w-0 grid-cols-2 rounded-lg bg-muted p-1">
                  <button
                    type="button"
                    onClick={() => setActivePanel('listing')}
                    className={`flex h-9 min-w-0 items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors ${activePanel === 'listing' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
                  >
                    <FileText className="h-4 w-4" /> Объявление
                  </button>
                  <button
                    type="button"
                    onClick={() => setActivePanel('pricing')}
                    className={`flex h-9 min-w-0 items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors ${activePanel === 'pricing' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
                  >
                    <BarChart3 className="h-4 w-4" /> Цены
                  </button>
                </div>
              </div>

              <div className="grid min-h-0 min-w-0 flex-1 grid-cols-[minmax(0,1fr)] overflow-hidden xl:grid-cols-[minmax(600px,1fr)_minmax(520px,560px)]">
                <section className={`${activePanel === 'pricing' ? 'block' : 'hidden'} min-h-0 min-w-0 max-w-full overflow-x-hidden overflow-y-auto overscroll-contain border-r xl:block`}>
                  <MarketPricingPanel
                    listingId={listing.id}
                    listingStatus={listing.status}
                    onApplyPrice={applyMarketPrice}
                    refreshKey={pricingRefreshKey}
                  />
                </section>

                <section className={`${activePanel === 'listing' ? 'block' : 'hidden'} min-h-0 min-w-0 max-w-full overflow-x-hidden overflow-y-auto overscroll-contain [scrollbar-gutter:stable] xl:block`}>
                  <div className="w-full min-w-0 max-w-full space-y-5 overflow-x-hidden p-4 pb-8 sm:p-5 sm:pb-8">
              <SheetHeader>
                <div className="flex min-w-0 items-start gap-3">
                  <SheetTitle className="min-w-0 flex-1 break-words pr-1 leading-tight [overflow-wrap:anywhere]">
                    <span className="mb-1 block min-w-0 truncate font-mono text-xs text-muted-foreground">
                      {listing.product_article}
                    </span>
                    {listing.product_name}
                  </SheetTitle>
                  <Badge variant={STATUS_VARIANT[listing.status] ?? 'outline'} className="max-w-[42%] shrink-0 whitespace-normal text-right leading-tight">
                    {listing.status_display}
                  </Badge>
                </div>
              </SheetHeader>

              {/* Фотографии — Avito-стиль */}
              <div className="space-y-2">
                {/* Главное фото */}
                <div
                  className="relative w-full rounded-lg overflow-hidden bg-muted border"
                  style={{ aspectRatio: '4/3' }}
                >
                  {activeImage?.url ? (
                    <button
                      type="button"
                      className="w-full h-full"
                      onClick={() => setPreviewImg(activeImage.url)}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={activeImage.url}
                        alt=""
                        className="w-full h-full object-contain"
                      />
                    </button>
                  ) : (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={getCategoryPlaceholder('', listing.product_name)}
                      alt=""
                      className="w-full h-full object-cover opacity-80"
                    />
                  )}
                  {/* Стрелки навигации */}
                  {listing.images.length > 1 && (
                    <>
                      <button
                        type="button"
                        onClick={() => setActiveIdx((i) => (i - 1 + listing.images.length) % listing.images.length)}
                        className="absolute left-2 top-1/2 -translate-y-1/2 bg-black/40 hover:bg-black/60 text-white rounded-full p-1 transition-colors"
                      >
                        <ChevronLeft className="h-4 w-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setActiveIdx((i) => (i + 1) % listing.images.length)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 bg-black/40 hover:bg-black/60 text-white rounded-full p-1 transition-colors"
                      >
                        <ChevronRight className="h-4 w-4" />
                      </button>
                      <span className="absolute bottom-2 right-2 bg-black/50 text-white text-xs px-2 py-0.5 rounded-full">
                        {activeIdx + 1} / {listing.images.length}
                      </span>
                    </>
                  )}
                </div>

                {/* Миниатюры + кнопка добавить */}
                <div className="flex gap-2 overflow-x-auto pb-1">
                  {listing.images.map((img, idx) => (
                    <div key={img.id} className="relative flex-shrink-0 group">
                      <button
                        type="button"
                        onClick={() => setActiveIdx(idx)}
                        className={`w-14 h-14 rounded-md overflow-hidden border-2 transition-colors bg-muted ${
                          idx === activeIdx ? 'border-primary' : 'border-transparent hover:border-muted-foreground/40'
                        }`}
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img src={img.thumb_url || img.url} alt="" className="w-full h-full object-cover" />
                      </button>
                      {img.is_primary && (
                        <Crown className="absolute top-0.5 left-0.5 h-3 w-3 text-yellow-500 drop-shadow" />
                      )}
                      {/* Управление — появляется при наведении */}
                      <div className="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity rounded-md flex items-center justify-center gap-0.5">
                        {!img.is_primary && (
                          img.id !== null && (
                            <button
                              type="button"
                              onClick={() => handleSetPrimary(img.id!)}
                              disabled={photoLoading === img.id}
                              className="p-1 bg-white/20 rounded hover:bg-white/40 disabled:opacity-50"
                              title="Сделать главным"
                            >
                              <Crown className="h-3 w-3 text-white" />
                            </button>
                          )
                        )}
                        {img.id !== null && (
                          <button
                            type="button"
                            onClick={() => handleDeletePhoto(img.id!)}
                            disabled={photoLoading === img.id}
                            className="p-1 bg-white/20 rounded hover:bg-red-500/70 disabled:opacity-50"
                            title="Удалить"
                          >
                            <Trash2 className="h-3 w-3 text-white" />
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                  {/* Добавить фото */}
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={photoLoading === 'upload'}
                    className="flex-shrink-0 w-14 h-14 rounded-md border-2 border-dashed border-muted-foreground/30 hover:border-primary/50 flex items-center justify-center transition-colors"
                  >
                    <Plus className="h-5 w-5 text-muted-foreground/50" />
                  </button>
                </div>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={handleUploadPhoto}
                />
              </div>

              {/* Уверенность AI */}
              <ConfidenceBar value={listing.ai_confidence} label={listing.ai_confidence_display} />

              {/* Заголовок */}
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <p className="text-sm text-muted-foreground">Заголовок</p>
                  {editing && (
                    <span className="text-xs text-muted-foreground">
                      {editTitle.length}/{AVITO_TITLE_MAX}
                    </span>
                  )}
                </div>
                {editing ? (
                  <>
                    <Input
                      value={editTitle}
                      maxLength={AVITO_TITLE_MAX}
                      onChange={(e) => setEditTitle(e.target.value)}
                    />
                    <p className="text-xs text-muted-foreground">
                      Лимит автозагрузки Avito — {AVITO_TITLE_MAX} символов
                    </p>
                  </>
                ) : (
                  <p className="min-w-0 break-words font-medium [overflow-wrap:anywhere]">{listing.title || '—'}</p>
                )}
              </div>

              {/* AI-описание */}
              <div className="space-y-1">
                <p className="text-sm text-muted-foreground">AI-описание</p>
                {editing ? (
                  <Textarea
                    value={editDesc}
                    rows={10}
                    onChange={(e) => setEditDesc(e.target.value)}
                    className="text-sm resize-none"
                  />
                ) : (
                  <pre className="max-h-48 min-w-0 max-w-full whitespace-pre-wrap break-words rounded-md bg-muted/40 p-3 font-sans text-sm leading-relaxed [overflow-wrap:anywhere] overflow-x-hidden overflow-y-auto">
                    {listing.description_ai || '—'}
                  </pre>
                )}
              </div>

              {/* Бренд хранится у товара и используется в выгрузке Avito. */}
              <div className="space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm text-muted-foreground">Бренд</p>
                  {!editingBrand && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="h-7 px-2"
                      onClick={() => {
                        setEditBrand(listing.product_brand || '');
                        setEditingBrand(true);
                      }}
                      disabled={busy}
                    >
                      <Pencil className="mr-1 h-3.5 w-3.5" />
                      Изменить
                    </Button>
                  )}
                </div>
                {editingBrand ? (
                  <div className="relative">
                    <Input
                      value={editBrand}
                      onChange={(e) => setEditBrand(e.target.value)}
                      onFocus={() => setBrandInputFocused(true)}
                      onBlur={() => setTimeout(() => setBrandInputFocused(false), 150)}
                      placeholder="Начните вводить бренд"
                      disabled={busy}
                    />
                    {brandInputFocused && (brandOptionsLoading || brandOptions.length > 0) && (
                      <div className="absolute z-20 mt-1 max-h-52 w-full overflow-y-auto rounded-md border bg-popover p-1 text-sm shadow-md">
                        {brandOptionsLoading ? (
                          <p className="px-2 py-1.5 text-muted-foreground">Ищем бренды…</p>
                        ) : brandOptions.map((option) => (
                          <button
                            type="button"
                            key={`${option.source}-${option.name}`}
                            className="flex min-w-0 w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-muted"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => {
                              setEditBrand(option.name);
                              setBrandInputFocused(false);
                            }}
                          >
                            <span className="min-w-0 break-words [overflow-wrap:anywhere]">{option.name}</span>
                            <span className="ml-3 shrink-0 text-xs text-muted-foreground">
                              {option.source === 'category' ? 'по категории' : option.source === 'avito' ? 'каталог Avito' : 'текущее'}
                            </span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="min-w-0 break-words font-medium [overflow-wrap:anywhere]">{listing.product_brand || '—'}</p>
                )}
                {editingBrand && (
                  <div className="flex gap-2 pt-1">
                    <Button size="sm" onClick={handleSaveBrand} disabled={busy}>
                      {actionLoading === 'brand' ? 'Сохраняем…' : 'Сохранить бренд'}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setEditBrand(listing.product_brand || '');
                        setEditingBrand(false);
                      }}
                      disabled={busy}
                    >
                      Отмена
                    </Button>
                  </div>
                )}
                <p className="text-xs text-muted-foreground">
                  {brandCategoryScope
                    ? `Сначала показаны бренды для категории «${brandCategoryScope}». `
                    : ''}
                  {avitoBrandCatalogLoaded
                    ? 'При вводе доступны совпадения из каталога Avito.'
                    : 'Подтягивается из товара и нужен для публикации на Avito.'}
                </p>
                {(avitoBrandCatalogSyncedAt || listing.avito_brand_catalog_synced_at) && (
                  <p className="text-xs text-muted-foreground">
                    Справочник Avito обновлён{' '}
                    {new Date(avitoBrandCatalogSyncedAt || listing.avito_brand_catalog_synced_at!).toLocaleDateString('ru-RU')}
                    {avitoBrandCatalogStale ? ' — требуется обновление' : ''}.
                  </p>
                )}
              </div>

              {/* Цена и аккаунт */}
              <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">Аккаунт Avito</p>
                  <select
                    value={editAccountId}
                    onChange={(e) => {
                      setEditAccountId(e.target.value);
                      setEditPlacementAddressId(defaultAddressIdForAccount(e.target.value));
                    }}
                    disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                    className="h-9 w-full min-w-0 rounded-md border bg-background px-3 text-sm"
                  >
                    {accounts.map((account) => (
                      <option key={account.id} value={account.id}>
                        {account.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">
                    Базовая цена (из товара)
                  </p>
                  <p className="h-9 flex items-center text-sm text-muted-foreground">
                    {listing.base_price
                      ? `${Number(listing.base_price).toLocaleString('ru-RU')} ₽`
                      : '—'}
                  </p>
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">Наценка, %</p>
                  <Input
                    value={editMarginPct}
                    onChange={(e) => {
                      const v = e.target.value;
                      setEditMarginPct(v);
                      const base = Number(listing.base_price);
                      const pct = Number(v);
                      if (base > 0 && v !== '' && !Number.isNaN(pct)) {
                        setEditPrice(String((base * (1 + pct / 100)).toFixed(2)));
                      }
                    }}
                    disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                    inputMode="decimal"
                    placeholder={
                      listing.catalog_category?.default_margin_pct
                        ? `${listing.catalog_category.default_margin_pct}% (по категории)`
                        : '0'
                    }
                    className="h-9 min-w-0 text-sm"
                  />
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">Цена объявления</p>
                  <Input
                    value={editPrice}
                    onChange={(e) => {
                      setEditPrice(e.target.value);
                      setEditMarginPct('');
                    }}
                    disabled={listing.status === 'deleted' || busy || (listing.status === 'active' && !marketPriceApplied)}
                    inputMode="decimal"
                    className="h-9 min-w-0 text-sm"
                  />
                  {marketPriceApplied && (
                    <p className="text-xs text-emerald-700 dark:text-emerald-300">
                      Рыночная цена подготовлена. Она применится только после сохранения.
                    </p>
                  )}
                </div>
              </div>

              <div className="space-y-1">
                <p className="text-sm text-muted-foreground">Вид объявления (Avito)</p>
                <select
                  value={editAdType}
                  onChange={(e) => setEditAdType(e.target.value)}
                  disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                  className="h-9 w-full min-w-0 rounded-md border bg-background px-3 text-sm"
                >
                  {AD_TYPE_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Единое официальное дерево Avito: назначить можно только активный лист. */}
              {categories.length > 0 ? (
                <div className="space-y-1 rounded-md border p-3">
                  <p className="text-sm text-muted-foreground">Категория Avito</p>
                  <CatalogCategoryPicker
                    categories={categories}
                    value={editCategoryId}
                    onValueChange={setEditCategoryId}
                    disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                    placeholder="Выберите категорию автозапчасти"
                    dropdownClassName="min-w-0 sm:min-w-0"
                  />
                </div>
              ) : listing.catalog_category ? (
                // Дерево категорий недоступно (домен каталога выключен), но категория
                // у товара определена — показываем её read-only, чтобы тенант видел.
                <div className="space-y-1 rounded-md border p-3">
                  <p className="text-sm text-muted-foreground">Категория</p>
                  <p className="min-w-0 break-words font-medium [overflow-wrap:anywhere]">
                    {listing.catalog_category.parent_name
                      ? `${listing.catalog_category.parent_name} → ${listing.catalog_category.name}`
                      : listing.catalog_category.name}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Чтобы изменить категорию, включите домен каталога в Настройках.
                  </p>
                </div>
              ) : null}

              <div className="space-y-2 rounded-md border p-3">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium">Размещение</p>
                    <p className="text-xs text-muted-foreground">
                      Адрес точки продаж из настроек Avito-аккаунта. Он попадёт в feed как место размещения.
                    </p>
                  </div>
                </div>
                <div className="grid min-w-0 gap-2">
                  <select
                    value={editPlacementAddressId}
                    onChange={(e) => setEditPlacementAddressId(e.target.value)}
                    className="h-8 w-full min-w-0 rounded-md border bg-background px-3 text-xs"
                  >
                    <option value="">Использовать адрес аккаунта или массовое правило</option>
                    {visiblePlacementAddresses.map((address) => (
                      <option key={address.id} value={address.id}>
                        {address.name}
                        {address.is_default ? ' · по умолчанию' : ''}
                        {address.seller_address_id ? ` · ID ${address.seller_address_id}` : ''}
                        {address.address ? ` · ${address.address}` : ''}
                      </option>
                    ))}
                  </select>
                  {selectedPlacementAddress && (
                    <div className="min-w-0 rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                      <p className="min-w-0 break-words font-medium text-foreground">{selectedPlacementAddress.name}</p>
                      <p className="min-w-0 break-words">
                        {selectedPlacementAddress.seller_address_id
                          ? `ID адреса Avito: ${selectedPlacementAddress.seller_address_id}`
                          : selectedPlacementAddress.address || 'Адрес без текстового значения'}
                      </p>
                      {(selectedPlacementAddress.contact_phone || selectedPlacementAddress.manager_name) && (
                        <p>
                          {[selectedPlacementAddress.manager_name, selectedPlacementAddress.contact_phone]
                            .filter(Boolean)
                            .join(' · ')}
                        </p>
                      )}
                    </div>
                  )}
                  {visiblePlacementAddresses.length === 0 && (
                    <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
                      Для выбранного аккаунта ещё нет адресов. Добавьте их в Настройки → Маркетплейсы → Avito.
                    </p>
                  )}
                  {!editPlacementAddressId && !editAddress && !editSellerAddressId &&
                    (listing.bulk_address || listing.bulk_seller_address_id) && (
                      <p className="text-xs text-muted-foreground">
                        Сейчас применится массовое размещение: {
                          listing.bulk_seller_address_id
                            ? `ID адреса Avito ${listing.bulk_seller_address_id}`
                            : listing.bulk_address
                        }
                      </p>
                    )}
                  {!editPlacementAddressId && !editAddress && !editSellerAddressId &&
                    !listing.bulk_address && !listing.bulk_seller_address_id && (
                      <p className="text-xs text-muted-foreground">
                        Сейчас применится адрес аккаунта или категории.
                      </p>
                    )}
                </div>
              </div>

              {/* Контакты объявления — из сохранённых адресов аккаунта (Настройки → Маркетплейсы) */}
              <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">Имя менеджера</p>
                  <select
                    value={editManagerName}
                    onChange={(e) => setEditManagerName(e.target.value)}
                    disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                    className="h-9 w-full min-w-0 rounded-md border bg-background px-3 text-sm"
                  >
                    <option value="">— из адреса / профиля —</option>
                    {editManagerName && !managerOptions.includes(editManagerName) && (
                      <option value={editManagerName}>{editManagerName}</option>
                    )}
                    {managerOptions.map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">Телефон</p>
                  <select
                    value={editContactPhone}
                    onChange={(e) => setEditContactPhone(e.target.value)}
                    disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                    className="h-9 w-full min-w-0 rounded-md border bg-background px-3 text-sm"
                  >
                    <option value="">— из адреса / профиля —</option>
                    {editContactPhone && !phoneOptions.includes(editContactPhone) && (
                      <option value={editContactPhone}>{editContactPhone}</option>
                    )}
                    {phoneOptions.map((phone) => (
                      <option key={phone} value={phone}>{phone}</option>
                    ))}
                  </select>
                </div>
                {managerOptions.length === 0 && phoneOptions.length === 0 && (
                  <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground sm:col-span-2">
                    Контакты ещё не сохранены. Добавьте их в Настройки → Маркетплейсы → Avito (адреса и контакты).
                  </p>
                )}
              </div>

              {/* Причина отклонения/проверки — только для этих статусов, иначе висит старый текст */}
              {listing.status === 'rejected' && listing.rejection_reason && (
                <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive whitespace-pre-line">
                  <span className="font-medium">Причина отклонения: </span>
                  {listing.rejection_reason}
                </div>
              )}
              {listing.status === 'requires_review' && listing.rejection_reason && (
                <div className="rounded-md border border-yellow-500/40 bg-yellow-500/10 p-3 text-sm text-yellow-700 dark:text-yellow-400 whitespace-pre-line">
                  <span className="font-medium">Требует проверки: </span>
                  {listing.rejection_reason}
                </div>
              )}

              {/* Незаполненные обязательные поля Avito — предупреждаем ДО публикации */}
              {(listing.avito_field_warnings?.length ?? 0) > 0 && (
                <div className="rounded-md border border-yellow-500/40 bg-yellow-500/10 p-3 text-sm text-yellow-700 dark:text-yellow-400">
                  <p className="font-medium">Перед публикацией проверьте данные:</p>
                  <ul className="mt-1 list-disc space-y-1 pl-4">
                    {listing.avito_field_warnings!.map((warning, i) => (
                      <li key={i}>{warning}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Кнопки действий */}
              <div className="sticky bottom-0 z-10 -mx-4 flex flex-col gap-2 border-t bg-background/95 px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur sm:-mx-5 sm:px-5">
                {editing ? (
                  <>
                    <Button onClick={handleSaveEdit} disabled={busy || editingBrand || (listing.status === 'active' && !marketPriceApplied)} className="w-full">
                      {actionLoading === 'save' ? 'Сохраняем...' : 'Сохранить'}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setEditing(false);
                        setEditTitle(listing.title);
                        setEditDesc(listing.description_ai);
                        setEditBrand(listing.product_brand || '');
                        setEditAccountId(String(listing.account_id));
                        setEditPrice(listing.price_on_listing);
                        setEditMarginPct(listing.margin_pct ?? listing.catalog_category?.default_margin_pct ?? '');
                        setMarketPriceApplied(false);
                        setEditAdType(listing.ad_type || DEFAULT_AD_TYPE);
                        setEditAddress(listing.address_override || '');
                        setEditSellerAddressId(listing.seller_address_id_override || '');
                        setEditManagerName(listing.manager_name_override || '');
                        setEditContactPhone(listing.contact_phone_override || '');
                        setEditPlacementAddressId(listing.placement_address ? String(listing.placement_address) : '');
                      }}
                      disabled={busy || editingBrand}
                      className="w-full"
                    >
                      Отмена
                    </Button>
                  </>
                ) : (
                  <>
                    <Button onClick={handleSaveEdit} disabled={busy || editingBrand || (listing.status === 'active' && !marketPriceApplied)} className="w-full">
                      {actionLoading === 'save' ? 'Сохраняем...' : 'Сохранить'}
                    </Button>
                    {isReview && listing.avito_brand_valid && (
                      <Button
                        onClick={handleApprove}
                        disabled={busy || editingBrand}
                        className="w-full bg-green-600 hover:bg-green-700 text-white"
                      >
                        <CheckCircle className="mr-2 h-4 w-4" />
                        {actionLoading === 'approve' ? 'Публикация...' : 'Одобрить и опубликовать'}
                      </Button>
                    )}
                    {isReview && !listing.avito_brand_valid && (
                      <div className="space-y-2">
                        <Button
                          type="button"
                          variant="outline"
                          className="w-full"
                          onClick={handleRefreshBrandCatalog}
                          disabled={busy || editingBrand}
                        >
                          <RefreshCw className={`mr-2 h-4 w-4 ${actionLoading === 'brand-catalog' ? 'animate-spin' : ''}`} />
                          Обновить справочник и проверить снова
                        </Button>
                        <p className="text-xs text-amber-700">
                          Публикация заблокирована: выберите бренд из справочника Avito. Если бренда нет,
                          запросите его добавление в поддержке Avito.
                        </p>
                      </div>
                    )}
                    {(canPublish || publishing) && (
                      <Button
                        onClick={handlePublish}
                        disabled={busy || publishing || editingBrand}
                        className="w-full"
                      >
                        {publishing
                          ? <><RefreshCw className="mr-2 h-4 w-4 animate-spin" />Публикуется...</>
                          : actionLoading === 'publish'
                            ? <><RefreshCw className="mr-2 h-4 w-4 animate-spin" />Отправка...</>
                            : <><Send className="mr-2 h-4 w-4" />Опубликовать</>
                        }
                      </Button>
                    )}
                    {isPending && (
                      <Button
                        variant="secondary"
                        onClick={handleCheckStatus}
                        disabled={busy || editingBrand}
                        className="w-full"
                      >
                        <RefreshCw className="mr-2 h-4 w-4" />
                        {actionLoading === 'checkStatus' ? 'Проверяем...' : 'Проверить статус Avito'}
                      </Button>
                    )}
                    {canRegenerate && (
                      <Button
                        variant="secondary"
                        onClick={handleRegenerate}
                        disabled={busy || editingBrand}
                        className="w-full"
                      >
                        <RefreshCw className="mr-2 h-4 w-4" />
                        {actionLoading === 'regenerate' ? 'Отправка задачи...' : 'Перегенерировать AI'}
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      onClick={() => setEditing(true)}
                      disabled={busy || editingBrand}
                      className="w-full"
                    >
                      <Pencil className="mr-2 h-4 w-4" />
                      Редактировать текст объявления
                    </Button>
                  </>
                )}
              </div>
                  </div>
                </section>
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>

      {/* Fullscreen предпросмотр */}
      {previewImg && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
          onClick={() => setPreviewImg(null)}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={previewImg}
            alt="Предпросмотр"
            className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain"
          />
        </div>
      )}
    </>
  );
}
