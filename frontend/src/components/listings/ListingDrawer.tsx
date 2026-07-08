'use client';

import { useState, useEffect, useRef } from 'react';
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
  ChevronLeft, ChevronRight, Send,
} from 'lucide-react';
import { getCategoryPlaceholder } from '@/lib/category-placeholder';

interface ListingImage {
  id: number | null;
  url: string;
  thumb_url: string;
  position: number;
  is_primary: boolean;
}

interface ListingDetail {
  id: number;
  status: string;
  status_display: string;
  product_id: number;
  product_article: string;
  product_name: string;
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
  images: ListingImage[];
  catalog_category: {
    id: number;
    name: string;
    parent_id: number | null;
    parent_name: string | null;
    default_margin_pct?: string;
  } | null;
}

interface CatalogCategory {
  id: number;
  name: string;
  parent: number | null;
  default_margin_pct?: string;
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

export default function ListingDrawer({ listingId, onClose, onActionDone }: Props) {
  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [editAddress, setEditAddress] = useState('');
  const [editSellerAddressId, setEditSellerAddressId] = useState('');
  const [editManagerName, setEditManagerName] = useState('');
  const [editContactPhone, setEditContactPhone] = useState('');
  const [editPlacementAddressId, setEditPlacementAddressId] = useState('');
  const [editAccountId, setEditAccountId] = useState('');
  const [editPrice, setEditPrice] = useState('');
  const [editMarginPct, setEditMarginPct] = useState<string>('');
  const [editAdType, setEditAdType] = useState(DEFAULT_AD_TYPE);
  const [categories, setCategories] = useState<CatalogCategory[]>([]);
  const [editCategoryId, setEditCategoryId] = useState('');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [placementAddresses, setPlacementAddresses] = useState<PlacementAddress[]>([]);
  const [previewImg, setPreviewImg] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [photoLoading, setPhotoLoading] = useState<number | 'upload' | null>(null);
  const [publishing, setPublishing] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const publishPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Листинг, для которого уже выполнена авто-подстановка адреса по умолчанию.
  const placementInitRef = useRef<number | null>(null);

  const open = listingId !== null;

  const applyListingState = (data: ListingDetail) => {
    setListing(data);
    setEditTitle(data.title);
    setEditDesc(data.description_ai);
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
  };

  useEffect(() => {
    if (listingId === null) {
      setListing(null);
      setEditing(false);
      setActiveIdx(0);
      setPublishing(false);
      placementInitRef.current = null;
      if (publishPollRef.current) clearInterval(publishPollRef.current);
      return;
    }
    setLoading(true);
    listingApi.get(listingId)
      .then((res) => {
        const data: ListingDetail = res.data.data;
        applyListingState(data);
        // Ставим главное фото активным
        const primaryIdx = data.images.findIndex((i) => i.is_primary);
        setActiveIdx(primaryIdx >= 0 ? primaryIdx : 0);
      })
      .catch(() => onClose())
      .finally(() => setLoading(false));
  }, [listingId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    accountApi.list()
      .then((res) => setAccounts(res.data.data ?? res.data))
      .catch(() => setAccounts([]));
    accountApi.listPlacementAddresses()
      .then((res) => setPlacementAddresses(res.data.data ?? res.data))
      .catch(() => setPlacementAddresses([]));
    productApi.catalogCategories()
      .then((res) => setCategories(res.data.data ?? res.data))
      .catch(() => setCategories([]));
  }, []);

  // Каскад произвольной глубины: путь от корня до выбранной категории + уровни выбора.
  const categoryById = new Map(categories.map((c) => [c.id, c]));
  const selectedAncestry = (() => {
    const out: number[] = [];
    let cur = editCategoryId ? categoryById.get(Number(editCategoryId)) : undefined;
    while (cur) {
      out.unshift(cur.id);
      cur = cur.parent ? categoryById.get(cur.parent) : undefined;
    }
    return out;
  })();
  const categoryLevels: { options: CatalogCategory[]; selectedId: number | null; idx: number }[] = [];
  {
    let parentId: number | null = null;
    for (let i = 0; ; i += 1) {
      const options = categories.filter((c) => (c.parent ?? null) === parentId);
      if (options.length === 0) break;
      const selectedId = selectedAncestry[i] ?? null;
      categoryLevels.push({ options, selectedId, idx: i });
      if (selectedId === null) break;
      parentId = selectedId;
    }
  }

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

  useEffect(() => {
    if (!editPlacementAddressId) return;
    const selected = placementAddresses.find((address) => address.id === Number(editPlacementAddressId));
    if (selected && editAccountId && selected.account !== Number(editAccountId)) {
      setEditPlacementAddressId('');
    }
  }, [editAccountId, editPlacementAddressId, placementAddresses]);

  // Авто-подстановка адреса по умолчанию при открытии листинга без явного адреса.
  // Тенант может затем выбрать другой адрес или «адрес аккаунта». Выполняется
  // один раз на листинг, чтобы не перетирать ручной выбор «без адреса».
  useEffect(() => {
    if (!listing || placementAddresses.length === 0) return;
    if (placementInitRef.current === listing.id) return;
    placementInitRef.current = listing.id;
    if (listing.placement_address || listing.address_override || listing.seller_address_id_override) return;
    const fallback = defaultAddressIdForAccount(String(listing.account_id));
    if (fallback) setEditPlacementAddressId(fallback);
  }, [listing, placementAddresses]); // eslint-disable-line react-hooks/exhaustive-deps

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
      await saveCategoryIfChanged();
      const res = await listingApi.updateContent(listing.id, buildEditPayload());
      applyListingState(res.data.data);
      setEditing(false);
      onActionDone();
      toast.success('Сохранено');
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
        <SheetContent side="right" className="w-full sm:max-w-[520px] overflow-y-auto">
          {loading || !listing ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              Загрузка...
            </div>
          ) : (
            <div className="space-y-5 pb-8">
              <SheetHeader>
                <div className="flex items-start gap-3">
                  <SheetTitle className="flex-1 leading-tight">
                    <span className="font-mono text-xs text-muted-foreground block mb-1">
                      {listing.product_article}
                    </span>
                    {listing.product_name}
                  </SheetTitle>
                  <Badge variant={STATUS_VARIANT[listing.status] ?? 'outline'}>
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
                  <p className="font-medium">{listing.title || '—'}</p>
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
                  <pre className="whitespace-pre-wrap text-sm bg-muted/40 rounded-md p-3 font-sans leading-relaxed max-h-48 overflow-y-auto">
                    {listing.description_ai || '—'}
                  </pre>
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
                    disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                    inputMode="decimal"
                    className="h-9 min-w-0 text-sm"
                  />
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

              {/* Категория и подкатегории любой глубины — проверить/изменить перед
                  публикацией. По ним строится категория Avito (GoodsType/SparePartType). */}
              {categoryLevels.length > 0 ? (
                <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
                  {categoryLevels.map(({ options, selectedId, idx }) => (
                    <div className="space-y-1" key={idx}>
                      <p className="text-sm text-muted-foreground">
                        {idx === 0 ? 'Категория' : `Подкатегория ${idx}`}
                      </p>
                      <select
                        value={selectedId ?? ''}
                        onChange={(e) => {
                          const v = e.target.value;
                          if (v) setEditCategoryId(v);
                          else setEditCategoryId(idx > 0 ? String(categoryLevels[idx - 1].selectedId) : '');
                        }}
                        disabled={listing.status === 'active' || listing.status === 'deleted' || busy}
                        className="h-9 w-full min-w-0 rounded-md border bg-background px-3 text-sm"
                      >
                        <option value="">{idx === 0 ? '— не задана —' : '— вся категория —'}</option>
                        {options.map((category) => (
                          <option key={category.id} value={category.id}>
                            {category.name}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              ) : listing.catalog_category ? (
                // Дерево категорий недоступно (домен каталога выключен), но категория
                // у товара определена — показываем её read-only, чтобы тенант видел.
                <div className="space-y-1 rounded-md border p-3">
                  <p className="text-sm text-muted-foreground">Категория</p>
                  <p className="font-medium">
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
                    <div className="rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
                      <p className="font-medium text-foreground">{selectedPlacementAddress.name}</p>
                      <p>
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
                  <p className="font-medium">Заполните перед публикацией:</p>
                  <ul className="mt-1 list-disc space-y-1 pl-4">
                    {listing.avito_field_warnings!.map((warning, i) => (
                      <li key={i}>{warning}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Кнопки действий */}
              <div className="flex flex-col gap-2 pt-2">
                {editing ? (
                  <>
                    <Button onClick={handleSaveEdit} disabled={busy} className="w-full">
                      {actionLoading === 'save' ? 'Сохраняем...' : 'Сохранить'}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setEditing(false);
                        setEditTitle(listing.title);
                        setEditDesc(listing.description_ai);
                        setEditAccountId(String(listing.account_id));
                        setEditPrice(listing.price_on_listing);
                        setEditAdType(listing.ad_type || DEFAULT_AD_TYPE);
                        setEditAddress(listing.address_override || '');
                        setEditSellerAddressId(listing.seller_address_id_override || '');
                        setEditManagerName(listing.manager_name_override || '');
                        setEditContactPhone(listing.contact_phone_override || '');
                        setEditPlacementAddressId(listing.placement_address ? String(listing.placement_address) : '');
                      }}
                      disabled={busy}
                      className="w-full"
                    >
                      Отмена
                    </Button>
                  </>
                ) : (
                  <>
                    <Button onClick={handleSaveEdit} disabled={busy} className="w-full">
                      {actionLoading === 'save' ? 'Сохраняем...' : 'Сохранить'}
                    </Button>
                    {isReview && (
                      <Button
                        onClick={handleApprove}
                        disabled={busy}
                        className="w-full bg-green-600 hover:bg-green-700 text-white"
                      >
                        <CheckCircle className="mr-2 h-4 w-4" />
                        {actionLoading === 'approve' ? 'Публикация...' : 'Одобрить и опубликовать'}
                      </Button>
                    )}
                    {(canPublish || publishing) && (
                      <Button
                        onClick={handlePublish}
                        disabled={busy || publishing}
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
                        disabled={busy}
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
                        disabled={busy}
                        className="w-full"
                      >
                        <RefreshCw className="mr-2 h-4 w-4" />
                        {actionLoading === 'regenerate' ? 'Отправка задачи...' : 'Перегенерировать AI'}
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      onClick={() => setEditing(true)}
                      disabled={busy}
                      className="w-full"
                    >
                      <Pencil className="mr-2 h-4 w-4" />
                      Редактировать текст объявления
                    </Button>
                  </>
                )}
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
