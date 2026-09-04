'use client';

import Link from 'next/link';
import { useMemo, useRef, useState, type RefObject } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Images,
  Loader2,
  Pencil,
  RefreshCw,
  Save,
  Send,
  Warehouse,
} from 'lucide-react';

import {
  OzonOfferPreparationCard,
  type OzonOfferPreparationCardHandle,
} from '@/components/products/OzonOfferPreparation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
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

export interface OzonEditorProduct {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  price: string;
  stock_qty: number;
  title_ai: string;
  description_ai: string;
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
  preparationRef: RefObject<OzonOfferPreparationCardHandle | null>;
  footerAction: 'save' | 'regenerate' | 'publish' | 'reconcile' | 'commerce' | null;
  onPreparationChange: (preparation: OzonOfferPreparation | null) => void;
  onSave: () => void;
  onRegenerate: () => void;
  onPublish: () => void;
  onReconcile: () => void;
  onSyncCommerce: () => void;
  onReload: () => void;
}

function rubles(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ₽`;
}

function imageStatusLabel(status: string): string {
  if (['auto_approved', 'manually_set', 'imported'].includes(status)) return 'Одобрено';
  if (status === 'rejected') return 'Отклонено';
  return 'На проверке';
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

export function OzonListingEditorPanel({
  product,
  account,
  preparation,
  summary,
  images,
  preparationRef,
  footerAction,
  onPreparationChange,
  onSave,
  onRegenerate,
  onPublish,
  onReconcile,
  onSyncCommerce,
  onReload,
}: Props) {
  const [activeImageIndex, setActiveImageIndex] = useState(0);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const imagesSectionRef = useRef<HTMLDivElement>(null);
  const commonDataRef = useRef<HTMLDivElement>(null);
  const displayedImageIndex = Math.min(activeImageIndex, Math.max(images.length - 1, 0));
  const activeImage = images[displayedImageIndex] ?? null;
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

  const operationErrors = useMemo(() => (
    preparation?.publication.latest_operation?.errors ?? []
  ), [preparation]);

  function scrollToIssue(field: string) {
    if (preparationRef.current?.focusField(field)) return;
    const target = field === 'images' ? imagesSectionRef.current : commonDataRef.current;
    target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
          {blockingIssues.length === 0 && recommendations.map((issue) => (
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

      <div ref={imagesSectionRef} className="space-y-2 rounded-lg border p-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium">Фотографии товара</p>
            <p className="text-xs text-muted-foreground">Общие для Avito, Ozon и следующих площадок.</p>
          </div>
          <Badge variant="outline">{images.length}</Badge>
        </div>
        <button
          type="button"
          disabled={!activeImage?.url}
          onClick={() => activeImage?.url && setPreviewImage(activeImage.url)}
          className="relative flex aspect-[4/3] w-full items-center justify-center overflow-hidden rounded-lg border bg-muted disabled:cursor-default"
        >
          {activeImage?.url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={activeImage.url} alt="" className="h-full w-full object-contain" />
          ) : (
            <div className="text-center text-sm text-muted-foreground">
              <Images className="mx-auto mb-2 h-7 w-7" />
              У товара пока нет фотографий
            </div>
          )}
        </button>
        {images.length > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-1">
            {images.map((image, index) => (
              <button
                key={image.id}
                type="button"
                title={imageStatusLabel(image.status)}
                onClick={() => setActiveImageIndex(index)}
                className={`h-14 w-14 shrink-0 overflow-hidden rounded-md border bg-muted ${
                  index === displayedImageIndex ? 'ring-2 ring-primary' : ''
                }`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={image.thumb_url || image.url} alt="" className="h-full w-full object-cover" />
              </button>
            ))}
          </div>
        )}
        <Button asChild size="sm" variant="outline" className="w-full">
          <Link href={`/dashboard/products/${product.id}?returnTo=%2Fdashboard%2Flistings`}>
            <Images className="mr-2 h-4 w-4" /> Проверить и изменить фотографии
          </Link>
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3">
        <div className="rounded-lg border p-3">
          <p className="text-xs text-muted-foreground">Базовая цена</p>
          <p className="mt-1 font-semibold tabular-nums">{rubles(product.price)}</p>
        </div>
        <div className="rounded-lg border p-3">
          <p className="text-xs text-muted-foreground">Остаток</p>
          <p className="mt-1 font-semibold tabular-nums">{product.stock_qty} шт.</p>
        </div>
        <div className="rounded-lg border p-3">
          <p className="text-xs text-muted-foreground">Бренд</p>
          <p className="mt-1 truncate font-semibold">{product.brand || 'Не указан'}</p>
        </div>
      </div>

      <div ref={commonDataRef} className="space-y-4 rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">Общие данные товара</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Заголовок, описание, бренд, упаковка и фото используются площадками совместно.
            Изменение здесь может повлиять и на Avito.
          </p>
        </div>
        <div className="rounded-md border bg-muted/20 p-3">
          <p className="text-xs text-muted-foreground">Заголовок</p>
          <p className="mt-1 text-sm font-medium leading-relaxed">
            {product.title_ai || product.name}
          </p>
        </div>
        <div className="rounded-md border bg-muted/20 p-3">
          <p className="text-xs text-muted-foreground">AI-описание</p>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
            {product.description_ai || 'Описание ещё не подготовлено.'}
          </p>
        </div>
        <Button asChild variant="outline" className="w-full">
          <Link href={`/dashboard/products/${product.id}?returnTo=%2Fdashboard%2Flistings`}>
            <Pencil className="mr-2 h-4 w-4" />
            Исправить общие данные, упаковку или налог
          </Link>
        </Button>
      </div>

      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-sm text-muted-foreground">
        Ниже — данные только кабинета «{account.name}»: цена Ozon, категория и
        характеристики. Они сохраняются отдельно и не меняют объявление Avito.
      </div>

      {preparation?.publication.latest_operation && (
        <div className={`rounded-lg border p-3 ${
          preparation.publication.latest_operation.state === 'succeeded'
            ? 'border-emerald-500/30 bg-emerald-500/5'
            : preparation.publication.latest_operation.state === 'failed'
              ? 'border-destructive/40 bg-destructive/5'
              : 'border-blue-500/30 bg-blue-500/5'
        }`}
        >
          <div className="flex items-start gap-2">
            {preparation.publication.latest_operation.state === 'succeeded'
              ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
              : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />}
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                {ozonPublicationStatusLabel(preparation)}
              </p>
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
      )}

      <OzonOfferPreparationCard
        ref={preparationRef}
        key={`${product.id}:${account.id}`}
        productId={product.id}
        accounts={[account]}
        onPreparationChange={onPreparationChange}
        showAccountSelector={false}
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

      {previewImage && (
        <button
          type="button"
          aria-label="Закрыть полноэкранный просмотр"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
          onClick={() => setPreviewImage(null)}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={previewImage}
            alt="Предпросмотр фотографии товара"
            className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain"
          />
        </button>
      )}
    </div>
  );
}
