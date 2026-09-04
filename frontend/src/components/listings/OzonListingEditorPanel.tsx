'use client';

import Link from 'next/link';
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  RefreshCw,
  Save,
  Send,
  Warehouse,
} from 'lucide-react';

import {
  OzonListingPriceEditor,
  type OzonListingPriceEditorHandle,
} from '@/components/listings/OzonListingPriceEditor';
import {
  ProductMediaManager,
  type ProductMediaAction,
} from '@/components/listings/ProductMediaManager';
import {
  ProductPhysicalProfileEditor,
  type ProductPhysicalProfileEditorHandle,
} from '@/components/listings/ProductPhysicalProfileEditor';
import {
  OzonOfferPreparationCard,
  type OzonOfferPreparationCardHandle,
} from '@/components/products/OzonOfferPreparation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SheetHeader } from '@/components/ui/sheet';
import type { OzonOfferPreparation, OzonPreflightIssue } from '@/lib/ozon-offer-preparation';
import {
  ozonCanReconcile,
  ozonPublicationActionLabel,
  ozonPublicationDisabled,
  ozonPublicationMessage,
  ozonPublicationStatusLabel,
} from '@/lib/ozon-offer-preparation';
import type { OzonWorkspaceSummary } from '@/lib/publication-workspace';
import type { ProductPhysicalProfile } from '@/lib/product-physical-profile';

export interface OzonEditorProduct {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  price: string;
  stock_qty: number;
  title_ai: string;
  description_ai: string;
  physical_profile: ProductPhysicalProfile;
}

export interface OzonEditorImage {
  id: number;
  status: string;
  is_primary: boolean;
  position: number;
  url: string;
  thumb_url: string;
}

interface OzonEditorAccount {
  id: number;
  name: string;
  marketplace: string;
  is_active: boolean;
}

interface Props {
  product: OzonEditorProduct;
  account: OzonEditorAccount;
  preparation: OzonOfferPreparation | null;
  summary: OzonWorkspaceSummary | null;
  images: OzonEditorImage[];
  mediaAction: ProductMediaAction;
  preparationRefreshToken: number;
  preparationRef: RefObject<OzonOfferPreparationCardHandle | null>;
  footerAction: 'save' | 'regenerate' | 'publish' | 'reconcile' | 'commerce' | null;
  onPreparationChange: (preparation: OzonOfferPreparation | null) => void;
  onUploadImage: (file: File) => Promise<void>;
  onApproveImage: (imageId: number) => Promise<void>;
  onRejectImage: (imageId: number) => Promise<void>;
  onSetPrimaryImage: (imageId: number) => Promise<void>;
  onDeleteImage: (imageId: number) => Promise<boolean>;
  onSaveBrand: (brand: string) => Promise<boolean>;
  onPhysicalProfileChange: (profile: ProductPhysicalProfile) => void;
  onSave: () => void;
  onRegenerate: () => void;
  onPublish: () => void;
  onReconcile: () => void;
  onSyncCommerce: () => void;
  onReload: () => void;
}

export interface OzonListingEditorPanelHandle {
  applyMarketPrice: (price: string) => boolean;
}

function rubles(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ₽`;
}

function localStatusLabel(summary: OzonWorkspaceSummary | null): string {
  if (!summary) return 'Загружается';
  if (summary.publication_status === 'published') return 'Опубликована';
  if (['queued', 'import_processing', 'moderation_pending'].includes(summary.publication_status)) {
    return 'Проверяется в Ozon';
  }
  if (summary.publication_status === 'outcome_unknown') return 'Ответ нужно сверить';
  if (['send_failed', 'not_accepted', 'import_failed', 'moderation_failed'].includes(summary.publication_status)) {
    return 'Ozon отклонил карточку';
  }
  if (summary.publication_status === 'manual_review') return 'Нужна ручная проверка';
  return 'Не отправлялась';
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'Ещё не сверялась';
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(new Date(value));
}

export const OzonListingEditorPanel = forwardRef<
OzonListingEditorPanelHandle,
Props
>(function OzonListingEditorPanel({
  product,
  account,
  preparation,
  summary,
  images,
  mediaAction,
  preparationRefreshToken,
  preparationRef,
  footerAction,
  onPreparationChange,
  onUploadImage,
  onApproveImage,
  onRejectImage,
  onSetPrimaryImage,
  onDeleteImage,
  onSaveBrand,
  onPhysicalProfileChange,
  onSave,
  onRegenerate,
  onPublish,
  onReconcile,
  onSyncCommerce,
  onReload,
}, ref) {
  const imagesSectionRef = useRef<HTMLDivElement>(null);
  const commonDataRef = useRef<HTMLDivElement>(null);
  const priceSectionRef = useRef<HTMLDivElement>(null);
  const physicalProfileRef = useRef<ProductPhysicalProfileEditorHandle>(null);
  const priceEditorRef = useRef<OzonListingPriceEditorHandle>(null);
  const [editingBrand, setEditingBrand] = useState(false);
  const [brandValue, setBrandValue] = useState(product.brand ?? '');
  const [savingBrand, setSavingBrand] = useState(false);
  const publication = preparation?.publication;
  const providerSku = publication?.provider_sku ?? summary?.provider_sku ?? null;
  const externalUrl = providerSku
    ? `https://www.ozon.ru/product/${providerSku}/`
    : summary?.external_url;
  const statusLabel = preparation
    ? ozonPublicationStatusLabel(preparation)
    : localStatusLabel(summary);
  const isPublished = publication?.status === 'published'
    || summary?.publication_status === 'published';
  const blockingIssues = preparation?.preflight.errors ?? [];
  const recommendations = preparation?.preflight.recommendations ?? [];
  const commonBlockers = blockingIssues.filter((issue) => (
    ['name', 'brand', 'description', 'stock'].includes(issue.field)
  ));
  const commonRecommendations = recommendations.filter((issue) => (
    ['name', 'brand', 'description', 'stock'].includes(issue.field)
  ));

  useEffect(() => {
    setBrandValue(product.brand ?? '');
    setEditingBrand(false);
  }, [product.brand]);

  useImperativeHandle(ref, () => ({
    applyMarketPrice: (price: string) => priceEditorRef.current?.applyMarketPrice(price) ?? false,
  }));

  const operationErrors = useMemo(() => (
    preparation?.publication.latest_operation?.errors ?? []
  ), [preparation]);

  function scrollToIssue(field: string) {
    if (field === 'price') {
      priceSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      priceEditorRef.current?.focus();
      return;
    }
    if (physicalProfileRef.current?.focusField(field)) return;
    if (preparationRef.current?.focusField(field)) return;
    const target = field === 'images' ? imagesSectionRef.current : commonDataRef.current;
    target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async function saveBrand() {
    setSavingBrand(true);
    try {
      if (await onSaveBrand(brandValue)) setEditingBrand(false);
    } finally {
      setSavingBrand(false);
    }
  }

  function issueButton(issue: OzonPreflightIssue) {
    return (
      <button
        key={`${issue.code}:${issue.field}`}
        type="button"
        onClick={() => scrollToIssue(issue.field)}
        className="w-full rounded-md border border-amber-500/40 bg-background p-2.5 text-left text-xs transition-colors hover:bg-amber-500/10"
      >
        <span className="font-medium text-amber-950 dark:text-amber-100">{issue.label}</span>
        <span className="mt-0.5 block text-muted-foreground">{issue.message}</span>
      </button>
    );
  }

  function publicationOperationStatus() {
    const operation = preparation?.publication.latest_operation;
    if (!operation || !preparation) return null;
    return (
      <div className={`rounded-lg border p-3 ${
        operation.state === 'succeeded'
          ? 'border-emerald-500/30 bg-emerald-500/5'
          : operation.state === 'failed'
            ? 'border-destructive/40 bg-destructive/5'
            : 'border-blue-500/30 bg-blue-500/5'
      }`}
      >
        <div className="flex items-start gap-2">
          {operation.state === 'succeeded'
            ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
            : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />}
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">{ozonPublicationStatusLabel(preparation)}</p>
            {preparation.publication.provider_status && (
              <p className="mt-1 text-xs text-muted-foreground">
                Статус Ozon: {preparation.publication.provider_status}
                {preparation.publication.moderation_status
                  ? ` · Модерация: ${preparation.publication.moderation_status}`
                  : ''}
              </p>
            )}
            {operationErrors.map((error, index) => (
              <button
                key={`${error.code}:${error.attribute_id ?? ''}:${index}`}
                type="button"
                onClick={() => scrollToIssue(
                  error.field || (error.attribute_id ? 'attributes' : 'readiness'),
                )}
                className="mt-2 w-full rounded-md border bg-background p-2 text-left text-xs hover:bg-muted/50"
              >
                <p className="font-medium text-destructive">{error.message}</p>
                {(error.field || error.attribute_id || error.provider_code) && (
                  <p className="mt-1 text-muted-foreground">
                    {[
                      error.field,
                      error.attribute_id ? `характеристика ${error.attribute_id}` : '',
                      error.provider_code,
                    ].filter(Boolean).join(' · ')}
                  </p>
                )}
              </button>
            ))}
          </div>
        </div>
        {ozonCanReconcile(preparation) && (
          <Button
            type="button"
            variant="outline"
            className="mt-3 w-full"
            onClick={onReconcile}
            disabled={footerAction !== null}
          >
            <RefreshCw className={`mr-2 h-4 w-4 ${
              footerAction === 'reconcile' ? 'animate-spin' : ''
            }`}
            />
            {footerAction === 'reconcile'
              ? 'Проверяем в Ozon...'
              : 'Проверить статус в Ozon'}
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="w-full min-w-0 max-w-full space-y-5 overflow-x-hidden p-4 pb-8 sm:p-5 sm:pb-8">
      <SheetHeader className="text-left">
        <div className="flex min-w-0 items-start gap-3">
          <h2 className="min-w-0 flex-1 break-words pr-1 text-lg font-semibold leading-tight text-foreground [overflow-wrap:anywhere]">
            <span className="mb-1 block truncate font-mono text-xs text-muted-foreground">
              {product.article}
            </span>
            {product.name}
          </h2>
          <Badge variant={isPublished ? 'default' : blockingIssues.length > 0 ? 'destructive' : 'outline'}>
            {statusLabel}
          </Badge>
          <Badge variant="outline">Ozon</Badge>
        </div>
      </SheetHeader>

      <div className="rounded-lg border bg-muted/20 p-3">
        <div className="grid gap-3 text-xs sm:grid-cols-2">
          <div>
            <p className="text-muted-foreground">Кабинет Ozon</p>
            <p className="mt-0.5 font-medium">{account.name}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Последняя сверка</p>
            <p className="mt-0.5 font-medium">
              {formatDateTime(publication?.last_provider_sync_at ?? summary?.last_provider_sync_at)}
            </p>
          </div>
          <div>
            <p className="text-muted-foreground">Статус Ozon</p>
            <p className="mt-0.5 font-medium">
              {publication?.provider_status || summary?.provider_status || 'Ещё не получен'}
              {(publication?.moderation_status || summary?.moderation_status)
                ? ` · ${publication?.moderation_status || summary?.moderation_status}`
                : ''}
            </p>
          </div>
          <div>
            <p className="text-muted-foreground">SKU Ozon</p>
            <p className="mt-0.5 font-medium">{providerSku ?? 'Появится после публикации'}</p>
          </div>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {externalUrl ? (
            <Button asChild size="sm" variant="outline">
              <a href={externalUrl} target="_blank" rel="noreferrer">
                <ExternalLink className="mr-2 h-4 w-4" /> Открыть в Ozon
              </a>
            </Button>
          ) : <span className="hidden sm:block" />}
          <Button type="button" size="sm" variant="outline" onClick={onReload}>
            <RefreshCw className="mr-2 h-4 w-4" /> Обновить данные
          </Button>
        </div>
      </div>

      {publicationOperationStatus()}

      {preparation && (blockingIssues.length > 0 || recommendations.length > 0) && (
        <div className={`space-y-2 rounded-lg border p-3 ${
          blockingIssues.length > 0
            ? 'border-amber-500/40 bg-amber-500/10'
            : 'border-blue-500/30 bg-blue-500/5'
        }`}
        >
          <div className="flex items-start gap-2">
            <AlertCircle className={`mt-0.5 h-4 w-4 shrink-0 ${
              blockingIssues.length > 0 ? 'text-amber-700' : 'text-blue-700'
            }`} />
            <div>
              <p className="text-sm font-medium">
                {blockingIssues.length > 0
                  ? `Нужно исправить перед отправкой: ${blockingIssues.length}`
                  : `Рекомендации MAP: ${recommendations.length}`}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Нажмите на пункт — MAP прокрутит к нужному разделу.
              </p>
            </div>
          </div>
          {blockingIssues.map(issueButton)}
          {recommendations.length > 0 && (
            <p className="pt-1 text-xs font-medium text-blue-900 dark:text-blue-100">
              Рекомендации — отправку не блокируют
            </p>
          )}
          {recommendations.map((issue) => (
            <button
              key={`${issue.code}:${issue.field}`}
              type="button"
              onClick={() => scrollToIssue(issue.field)}
              className="w-full rounded-md border border-blue-500/30 bg-background p-2.5 text-left text-xs hover:bg-blue-500/10"
            >
              <span className="font-medium">{issue.label}</span>
              <span className="mt-0.5 block text-muted-foreground">{issue.message}</span>
            </button>
          ))}
        </div>
      )}

      <div
        data-testid="ozon-guided-workflow"
        className="space-y-3 rounded-lg border border-sky-500/25 bg-sky-500/5 p-3"
      >
        <div>
          <p className="text-sm font-medium">Как подготовить карточку</p>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            Работайте по блокам сверху вниз. Все обязательные данные редактируются прямо
            в этом окне; переход в товар нужен только для повторного обогащения.
          </p>
        </div>
        <div className="grid gap-2 text-xs sm:grid-cols-3">
          <div className="rounded-md border border-emerald-500/35 bg-background p-2.5">
            <strong className="text-emerald-800 dark:text-emerald-200">Готово автоматически</strong>
            <p className="mt-1 text-muted-foreground">
              Точный бренд и партномер, расчётная цена и остаток из 1С.
            </p>
          </div>
          <div className="rounded-md border border-blue-500/35 bg-background p-2.5">
            <strong className="text-blue-800 dark:text-blue-200">Проверить результат MAP</strong>
            <p className="mt-1 text-muted-foreground">
              Заголовок, описание, фотографии, категорию и найденные размеры упаковки.
            </p>
          </div>
          <div className="rounded-md border border-amber-500/40 bg-background p-2.5">
            <strong className="text-amber-800 dark:text-amber-200">Берём из документов</strong>
            <p className="mt-1 text-muted-foreground">
              Штрихкод без источника, ТН ВЭД, маркировку и НДС.
            </p>
          </div>
        </div>
      </div>

      <div ref={imagesSectionRef}>
        <ProductMediaManager
          images={images}
          action={mediaAction}
          onUpload={onUploadImage}
          onApprove={onApproveImage}
          onReject={onRejectImage}
          onSetPrimary={onSetPrimaryImage}
          onDelete={onDeleteImage}
        />
      </div>

      <div
        ref={commonDataRef}
        data-testid="ozon-common-product-section"
        className={`space-y-4 rounded-lg border border-l-4 p-4 ${
          commonBlockers.length > 0
            ? 'border-amber-500/50 bg-amber-500/5'
            : 'border-blue-500/40 bg-blue-500/5'
        }`}
      >
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="text-sm font-medium">2. Общие данные товара</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Заголовок и описание заполняет обогащение — их нужно проверить. Бренд можно
              исправить здесь. Эти данные общие для всех площадок.
            </p>
          </div>
          <Badge
            variant="outline"
            className={commonBlockers.length > 0
              ? 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'
              : 'border-blue-500/40 bg-blue-500/10 text-blue-900 dark:text-blue-100'}
          >
            {commonBlockers.length > 0
              ? `Нужно заполнить: ${commonBlockers.length}`
              : commonRecommendations.length > 0
                ? `Проверить: ${commonRecommendations.length}`
                : 'Проверьте перед отправкой'}
          </Badge>
        </div>
        <div className="space-y-1 rounded-md border border-blue-500/30 bg-background p-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">Заголовок после обогащения</p>
            <Badge variant="outline" className="border-blue-500/30 text-blue-700">
              Проверить
            </Badge>
          </div>
          <p className="min-w-0 break-words font-medium leading-relaxed [overflow-wrap:anywhere]">
            {product.title_ai || product.name}
          </p>
        </div>
        <div className={`space-y-1 rounded-md border bg-background p-3 ${
          product.description_ai ? 'border-blue-500/30' : 'border-amber-500/50'
        }`}
        >
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">AI-описание после обогащения</p>
            <Badge
              variant="outline"
              className={product.description_ai
                ? 'border-blue-500/30 text-blue-700'
                : 'border-amber-500/50 text-amber-800'}
            >
              {product.description_ai ? 'Проверить' : 'Рекомендуется заполнить'}
            </Badge>
          </div>
          <p className="max-h-48 min-w-0 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
            {product.description_ai || 'Описание ещё не подготовлено.'}
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className={`space-y-2 rounded-md border bg-background p-3 ${
            product.brand ? 'border-emerald-500/30' : 'border-amber-500/50'
          }`}
          >
            <div className="flex items-center justify-between gap-2">
              <label htmlFor="ozon-common-brand" className="text-sm text-muted-foreground">
                Бренд
              </label>
              {!editingBrand && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2"
                  onClick={() => setEditingBrand(true)}
                >
                  Изменить
                </Button>
              )}
            </div>
            {editingBrand ? (
              <>
                <Input
                  id="ozon-common-brand"
                  value={brandValue}
                  maxLength={200}
                  disabled={savingBrand}
                  placeholder="Укажите бренд товара"
                  onChange={(event) => setBrandValue(event.target.value)}
                />
                <div className="flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    disabled={savingBrand}
                    onClick={() => void saveBrand()}
                  >
                    {savingBrand && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                    Сохранить
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={savingBrand}
                    onClick={() => {
                      setBrandValue(product.brand ?? '');
                      setEditingBrand(false);
                    }}
                  >
                    Отмена
                  </Button>
                </div>
              </>
            ) : (
              <p className="font-medium">{product.brand || 'Нужно заполнить'}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Общий бренд товара: изменение используется и в Avito.
            </p>
          </div>
          <div className="rounded-md border bg-background p-3">
            <p className="text-sm text-muted-foreground">Остаток товара</p>
            <p className="mt-1 font-medium tabular-nums">{product.stock_qty} шт.</p>
            <p className="mt-2 text-xs text-muted-foreground">Приходит из учётной системы.</p>
          </div>
        </div>
        <Button asChild variant="outline" className="w-full">
          <Link href={`/dashboard/products/${product.id}?returnTo=%2Fdashboard%2Flistings`}>
            <ExternalLink className="mr-2 h-4 w-4" />
            Открыть товар и обогащение
          </Link>
        </Button>
      </div>

      <div ref={priceSectionRef}>
        <OzonListingPriceEditor
          ref={priceEditorRef}
          productId={product.id}
          accountId={account.id}
          accountName={account.name}
          stockQty={product.stock_qty}
          preparation={preparation}
          onPreparationChange={onPreparationChange}
        />
      </div>

      <ProductPhysicalProfileEditor
        ref={physicalProfileRef}
        productId={product.id}
        profile={product.physical_profile}
        onProfileChange={onPhysicalProfileChange}
      />

      <div className="rounded-lg border border-blue-500/25 bg-blue-500/5 p-3 text-xs text-blue-950 dark:text-blue-100">
        Следующий блок относится только к Ozon: выберите категорию и проверьте обязательные
        характеристики. Поля Avito от этого не изменятся.
      </div>

      <OzonOfferPreparationCard
        ref={preparationRef}
        key={`${product.id}:${account.id}`}
        productId={product.id}
        accounts={[account]}
        onPreparationChange={onPreparationChange}
        showAccountSelector={false}
        showPricing={false}
        showReadinessSummary={false}
        title="5. Категория и характеристики Ozon"
        refreshToken={preparationRefreshToken}
        embedded
      />

      {preparation?.publication.status === 'published' && (
        <div className="rounded-lg border border-violet-500/25 bg-violet-500/5 p-4">
          <div className="flex items-start gap-2">
            <Warehouse className="mt-0.5 h-4 w-4 text-violet-600" />
            <div>
              <p className="text-sm font-medium">Цена и остаток Ozon</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Склад: {preparation.commerce.warehouse_name || preparation.commerce.warehouse_id || 'не выбран'}
              </p>
            </div>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
            <div className="rounded-md border bg-background p-2">
              <p className="text-muted-foreground">Цена MAP</p>
              <p className="mt-1 font-medium">{preparation.commerce.desired_price ? rubles(preparation.commerce.desired_price) : '—'}</p>
              <p className="mt-1 text-muted-foreground">В Ozon: {preparation.commerce.last_synced_price ? rubles(preparation.commerce.last_synced_price) : 'не подтверждена'}</p>
            </div>
            <div className="rounded-md border bg-background p-2">
              <p className="text-muted-foreground">Остаток MAP</p>
              <p className="mt-1 font-medium">{preparation.commerce.desired_stock} шт.</p>
              <p className="mt-1 text-muted-foreground">В Ozon: {preparation.commerce.last_synced_stock ?? 'не подтверждён'}</p>
            </div>
          </div>
          {[preparation.commerce.price_operation, preparation.commerce.stock_operation]
            .filter((operation) => operation && ['failed', 'manual_review', 'outcome_unknown'].includes(operation.state))
            .map((operation) => (
              <p key={operation!.id} className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs">
                {operation!.errors[0]?.message || 'Результат Ozon требует ручной проверки.'}
              </p>
            ))}
          <Button
            type="button"
            variant="outline"
            className="mt-3 w-full"
            onClick={onSyncCommerce}
            disabled={footerAction !== null || !preparation.commerce.can_sync}
          >
            {footerAction === 'commerce'
              ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              : <RefreshCw className="mr-2 h-4 w-4" />}
            {footerAction === 'commerce' ? 'Синхронизируем...' : 'Синхронизировать цену и остаток'}
          </Button>
        </div>
      )}

      <div className="sticky bottom-0 z-10 -mx-4 space-y-2 border-t bg-background/95 px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur sm:-mx-5 sm:px-5">
        <p className="text-sm font-medium">Действия с карточкой Ozon</p>
        <Button
          type="button"
          className="w-full"
          onClick={onSave}
          disabled={footerAction !== null || !preparation?.draft || !preparation.schema}
        >
          {footerAction === 'save'
            ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            : <Save className="mr-2 h-4 w-4" />}
          {footerAction === 'save'
            ? 'Сохраняем характеристики...'
            : 'Сохранить характеристики и проверить'}
        </Button>
        <Button
          type="button"
          className="w-full"
          onClick={onPublish}
          disabled={footerAction !== null || ozonPublicationDisabled(preparation)}
        >
          {footerAction === 'publish'
            ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            : <Send className="mr-2 h-4 w-4" />}
          {footerAction === 'publish'
            ? 'Отправляем безопасно...'
            : ozonPublicationActionLabel(preparation)}
        </Button>
        <p className={`rounded-md border p-2.5 text-xs ${
          preparation?.preflight.ready
            ? 'border-blue-500/30 bg-blue-500/5 text-blue-950 dark:text-blue-100'
            : 'border-amber-500/40 bg-amber-500/10 text-amber-950 dark:text-amber-100'
        }`}
        >
          {ozonPublicationMessage(preparation)}
        </p>
        <Button
          type="button"
          variant="secondary"
          className="w-full"
          onClick={onRegenerate}
          disabled={footerAction !== null}
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${
            footerAction === 'regenerate' ? 'animate-spin' : ''
          }`}
          />
          {footerAction === 'regenerate' ? 'Запускаем обогащение...' : 'Перегенерировать AI'}
        </Button>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Категория сохраняется при выборе, цена — кнопкой в блоке цены,
          характеристики — кнопкой выше. Отправка в Ozon всегда отдельное действие.
        </p>
      </div>

    </div>
  );
});
