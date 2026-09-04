'use client';

import { useMemo, useRef, useState, type ChangeEvent } from 'react';
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Crown,
  ImageOff,
  Loader2,
  Plus,
  Trash2,
  X,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

export interface ProductMediaImage {
  id: number;
  status: string;
  is_primary: boolean;
  position: number;
  url: string;
  thumb_url: string;
}

export type ProductMediaAction = number | 'upload' | null;

interface Props {
  images: ProductMediaImage[];
  action: ProductMediaAction;
  onUpload: (file: File) => Promise<void>;
  onApprove: (imageId: number) => Promise<void>;
  onReject: (imageId: number) => Promise<void>;
  onSetPrimary: (imageId: number) => Promise<void>;
  onDelete: (imageId: number) => Promise<boolean>;
}

const REVIEW_STATUSES = new Set(['needs_review', 'low_confidence']);
const APPROVED_STATUSES = new Set(['auto_approved', 'manually_set', 'imported']);

function statusPresentation(status: string): {
  label: string;
  className: string;
} {
  if (APPROVED_STATUSES.has(status)) {
    return {
      label: 'Одобрено',
      className: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200',
    };
  }
  if (status === 'rejected') {
    return {
      label: 'Отклонено',
      className: 'border-red-500/40 bg-red-500/10 text-red-900 dark:text-red-100',
    };
  }
  return {
    label: 'Нужно проверить',
    className: 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100',
  };
}

export function ProductMediaManager({
  images,
  action,
  onUpload,
  onApprove,
  onReject,
  onSetPrimary,
  onDelete,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [activeImageId, setActiveImageId] = useState<number | null>(null);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ProductMediaImage | null>(null);

  const orderedImages = useMemo(() => (
    images.slice().sort((left, right) => (
      Number(right.is_primary) - Number(left.is_primary)
      || left.position - right.position
      || left.id - right.id
    ))
  ), [images]);

  const selectedIndex = orderedImages.findIndex((image) => image.id === activeImageId);
  const primaryIndex = orderedImages.findIndex((image) => image.is_primary);
  const activeIndex = selectedIndex >= 0 ? selectedIndex : Math.max(primaryIndex, 0);
  const activeImage = orderedImages[activeIndex] ?? null;
  const activeStatus = activeImage ? statusPresentation(activeImage.status) : null;
  const pendingReviewCount = orderedImages.filter((image) => REVIEW_STATUSES.has(image.status)).length;
  const hasNoImages = orderedImages.length === 0;

  function moveImage(direction: -1 | 1) {
    if (orderedImages.length < 2) return;
    const nextIndex = (activeIndex + direction + orderedImages.length) % orderedImages.length;
    setActiveImageId(orderedImages[nextIndex].id);
  }

  async function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (file) await onUpload(file);
  }

  return (
    <div className={`space-y-3 rounded-lg border border-l-4 p-3 ${
      hasNoImages
        ? 'border-red-500/50 bg-red-500/5'
        : pendingReviewCount > 0
          ? 'border-amber-500/50 bg-amber-500/5'
        : 'border-emerald-500/35 bg-emerald-500/5'
    }`} data-testid="shared-product-media-manager">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">1. Фотографии товара</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Одни и те же фотографии используются в Avito, Ozon и следующих маркетплейсах.
          </p>
        </div>
        <Badge
          variant="outline"
          className={hasNoImages
            ? 'border-red-500/50 bg-red-500/10 text-red-900 dark:text-red-100'
            : pendingReviewCount > 0
              ? 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'
            : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200'}
        >
          {hasNoImages
            ? 'Нужно добавить фото'
            : pendingReviewCount > 0
              ? `Нужно проверить: ${pendingReviewCount}`
            : `Готово: ${orderedImages.length}`}
        </Badge>
      </div>

      <div className="relative aspect-[4/3] w-full overflow-hidden rounded-lg border bg-muted">
        {activeImage?.url ? (
          <button
            type="button"
            className="h-full w-full"
            onClick={() => setPreviewImage(activeImage.url)}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={activeImage.url} alt="" className="h-full w-full object-contain" />
          </button>
        ) : (
          <div className="flex h-full flex-col items-center justify-center text-center text-sm text-muted-foreground">
            <ImageOff className="mb-2 h-7 w-7 opacity-40" />
            Фотографии не загружены
          </div>
        )}
        {orderedImages.length > 1 && (
          <>
            <button
              type="button"
              aria-label="Предыдущая фотография"
              onClick={() => moveImage(-1)}
              className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-black/40 p-1 text-white transition-colors hover:bg-black/60"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <button
              type="button"
              aria-label="Следующая фотография"
              onClick={() => moveImage(1)}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-black/40 p-1 text-white transition-colors hover:bg-black/60"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
            <span className="absolute bottom-2 right-2 rounded-full bg-black/50 px-2 py-0.5 text-xs text-white">
              {activeIndex + 1} / {orderedImages.length}
            </span>
          </>
        )}
      </div>

      <div className="flex gap-2 overflow-x-auto pb-1">
        {orderedImages.map((image) => {
          const status = statusPresentation(image.status);
          return (
            <button
              key={image.id}
              type="button"
              title={status.label}
              onClick={() => setActiveImageId(image.id)}
              className={`relative h-14 w-14 shrink-0 overflow-hidden rounded-md border-2 bg-muted transition-colors ${
                image.id === activeImage?.id
                  ? 'border-primary'
                  : 'border-transparent hover:border-muted-foreground/40'
              }`}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.thumb_url || image.url} alt="" className="h-full w-full object-cover" />
              {image.is_primary && (
                <Crown className="absolute left-0.5 top-0.5 h-3 w-3 text-yellow-500 drop-shadow" />
              )}
              {REVIEW_STATUSES.has(image.status) && (
                <span className="absolute bottom-0.5 right-0.5 h-2.5 w-2.5 rounded-full border border-white bg-amber-500" />
              )}
            </button>
          );
        })}
        <button
          type="button"
          aria-label="Загрузить фотографию"
          title="Загрузить фотографию"
          onClick={() => inputRef.current?.click()}
          disabled={action !== null}
          className="flex h-14 w-14 shrink-0 items-center justify-center rounded-md border-2 border-dashed border-muted-foreground/30 transition-colors hover:border-primary/50 disabled:opacity-50"
        >
          {action === 'upload'
            ? <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            : <Plus className="h-5 w-5 text-muted-foreground/60" />}
        </button>
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(event) => void handleUpload(event)}
        />
      </div>

      {activeImage && activeStatus && (
        <div className="space-y-2 rounded-md border bg-background p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Badge variant="outline" className={activeStatus.className}>
              {activeImage.is_primary ? 'Главное · ' : ''}{activeStatus.label}
            </Badge>
            <span className="text-xs text-muted-foreground">
              Фото {activeIndex + 1} из {orderedImages.length}
            </span>
          </div>
          {REVIEW_STATUSES.has(activeImage.status) && (
            <p className="text-xs text-amber-900 dark:text-amber-100">
              Проверьте, что на фото именно этот товар. Затем одобрите или отклоните его.
            </p>
          )}
          <div className="grid grid-cols-2 gap-2">
            {REVIEW_STATUSES.has(activeImage.status) && (
              <>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="text-emerald-700"
                  disabled={action !== null}
                  onClick={() => void onApprove(activeImage.id)}
                >
                  <Check className="mr-1.5 h-4 w-4" /> Одобрить
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="text-destructive"
                  disabled={action !== null}
                  onClick={() => void onReject(activeImage.id)}
                >
                  <X className="mr-1.5 h-4 w-4" /> Отклонить
                </Button>
              </>
            )}
            {!activeImage.is_primary && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={action !== null}
                onClick={() => void onSetPrimary(activeImage.id)}
              >
                <Crown className="mr-1.5 h-4 w-4" /> Сделать главным
              </Button>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="text-destructive"
              disabled={action !== null}
              onClick={() => setPendingDelete(activeImage)}
            >
              <Trash2 className="mr-1.5 h-4 w-4" /> Удалить
            </Button>
          </div>
        </div>
      )}

      <Button
        type="button"
        variant="outline"
        className="w-full"
        onClick={() => inputRef.current?.click()}
        disabled={action !== null}
      >
        {action === 'upload'
          ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          : <Plus className="mr-2 h-4 w-4" />}
        Загрузить фото
      </Button>

      <Dialog open={pendingDelete !== null} onOpenChange={(open) => { if (!open) setPendingDelete(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Удалить эту фотографию?</DialogTitle>
            <DialogDescription>
              Она исчезнет из общего товара и больше не попадёт ни в Avito, ни в Ozon.
            </DialogDescription>
          </DialogHeader>
          {pendingDelete?.url && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={pendingDelete.url}
              alt="Фото, выбранное для удаления"
              className="max-h-72 w-full rounded-lg border bg-muted object-contain"
            />
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPendingDelete(null)}>
              Отмена
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={action !== null}
              onClick={async () => {
                if (!pendingDelete) return;
                if (await onDelete(pendingDelete.id)) setPendingDelete(null);
              }}
            >
              {action === pendingDelete?.id && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Удалить фото
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
