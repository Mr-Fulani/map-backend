'use client';

import Link from 'next/link';
import { useState, type RefObject } from 'react';
import { AlertCircle, CheckCircle2, Images, Loader2, Pencil, RefreshCw, Save, Send, Warehouse } from 'lucide-react';

import {
  OzonOfferPreparationCard,
  type OzonOfferPreparationCardHandle,
} from '@/components/products/OzonOfferPreparation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { SheetHeader } from '@/components/ui/sheet';
import type { OzonOfferPreparation } from '@/lib/ozon-offer-preparation';
import {
  ozonCanReconcile,
  ozonPublicationActionLabel,
  ozonPublicationDisabled,
  ozonPublicationMessage,
  ozonPublicationStatusLabel,
} from '@/lib/ozon-offer-preparation';

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
  images: OzonEditorImage[];
  preparationRef: RefObject<OzonOfferPreparationCardHandle | null>;
  footerAction: 'save' | 'regenerate' | 'publish' | 'reconcile' | 'commerce' | null;
  onPreparationChange: (preparation: OzonOfferPreparation | null) => void;
  onSave: () => void;
  onRegenerate: () => void;
  onPublish: () => void;
  onReconcile: () => void;
  onSyncCommerce: () => void;
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

export function OzonListingEditorPanel({
  product,
  account,
  preparation,
  images,
  preparationRef,
  footerAction,
  onPreparationChange,
  onSave,
  onRegenerate,
  onPublish,
  onReconcile,
  onSyncCommerce,
}: Props) {
  const [activeImageIndex, setActiveImageIndex] = useState(0);
  const activeImage = images[activeImageIndex] ?? null;

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
          <Badge variant={preparation?.preflight.ready ? 'default' : 'outline'}>
            {preparation?.preflight.ready ? 'Готово' : 'Черновик'}
          </Badge>
          <Badge variant="outline">Ozon</Badge>
        </div>
      </SheetHeader>

      <div className="space-y-2">
        <div className="relative flex aspect-[4/3] w-full items-center justify-center overflow-hidden rounded-lg border bg-muted">
          {activeImage?.url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={activeImage.url} alt="" className="h-full w-full object-contain" />
          ) : (
            <div className="text-center text-sm text-muted-foreground">
              <Images className="mx-auto mb-2 h-7 w-7" />
              У товара пока нет фотографий
            </div>
          )}
        </div>
        {images.length > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-1">
            {images.map((image, index) => (
              <button
                key={image.id}
                type="button"
                title={imageStatusLabel(image.status)}
                onClick={() => setActiveImageIndex(index)}
                className={`h-14 w-14 shrink-0 overflow-hidden rounded-md border bg-muted ${
                  index === activeImageIndex ? 'ring-2 ring-primary' : ''
                }`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={image.thumb_url || image.url} alt="" className="h-full w-full object-cover" />
              </button>
            ))}
          </div>
        )}
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

      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-sm text-muted-foreground">
        Редактируйте здесь карточку кабинета «{account.name}». Поля Ozon, цена и
        характеристики сохраняются отдельно и не меняют объявление Avito.
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
              {preparation.publication.latest_operation.errors.map((error, index) => (
                <div
                  key={`${error.code}:${error.attribute_id ?? ''}:${index}`}
                  className="mt-2 rounded-md border bg-background p-2 text-xs"
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
                </div>
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

      <details className="rounded-lg border bg-muted/20 p-3">
        <summary className="cursor-pointer text-sm font-medium">Общие данные после обогащения</summary>
        <div className="mt-4 space-y-4 border-t pt-4">
          <div>
            <p className="text-xs text-muted-foreground">Заголовок</p>
            <p className="mt-1 text-sm font-medium leading-relaxed">
              {product.title_ai || product.name}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">AI-описание</p>
            <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
              {product.description_ai || 'Описание ещё не подготовлено.'}
            </p>
          </div>
          <Button asChild variant="outline" className="w-full">
            <Link href={`/dashboard/products/${product.id}?returnTo=%2Fdashboard%2Flistings`}>
              <Pencil className="mr-2 h-4 w-4" />
              Редактировать общие данные товара
            </Link>
          </Button>
        </div>
      </details>

      <div className="space-y-2 border-t pt-4">
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
          {footerAction === 'save' ? 'Проверяем и сохраняем...' : 'Проверить и сохранить'}
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
      </div>
    </div>
  );
}
