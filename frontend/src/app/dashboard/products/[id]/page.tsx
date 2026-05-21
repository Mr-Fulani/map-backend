'use client';

import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import { productApi, imageApi } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Separator } from '@/components/ui/separator';
import { toast } from 'sonner';
import {
  ArrowLeft,
  RefreshCw,
  Archive,
  Upload,
  Loader2,
  Package,
  ImageOff,
  Search,
  Crown,
  Check,
  X,
  Trash2,
} from 'lucide-react';

interface ProductImage {
  id: number;
  status: string;
  source_id: string | null;
  quality_score: number | null;
  is_primary: boolean;
  position: number;
  url: string;
  thumb_url: string;
  url_source: string | null;
}

interface ProductDetail {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  category_1c: string | null;
  condition: string;
  price: string;
  stock_qty: number;
  warehouse: string | null;
  export_enabled: boolean;
  sync_at: string | null;
  created_at: string;
  updated_at: string;
}

const CONDITION_LABELS: Record<string, string> = {
  new: 'Новый',
  used: 'Б/у',
  refurbished: 'Восстановленный',
};

const IMAGE_STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  auto_approved: 'default',
  manually_set: 'default',
  needs_review: 'secondary',
  low_confidence: 'outline',
  rejected: 'destructive',
};

const IMAGE_STATUS_LABELS: Record<string, string> = {
  auto_approved: 'Одобрено',
  manually_set: 'Вручную',
  needs_review: 'На проверке',
  low_confidence: 'Низкое качество',
  rejected: 'Отклонено',
};

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-2">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-right text-sm font-medium">{value ?? '—'}</span>
    </div>
  );
}

export default function ProductDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const [images, setImages] = useState<ProductImage[]>([]);
  const [imagesLoading, setImagesLoading] = useState(false);
  const [imageActionId, setImageActionId] = useState<number | null>(null);

  const [searchTaskId, setSearchTaskId] = useState<string | null>(null);
  const searching = searchTaskId !== null;

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [previewImg, setPreviewImg] = useState<string | null>(null);

  useEffect(() => {
    productApi
      .get(Number(id))
      .then((res) => setProduct(res.data.data))
      .catch(() => toast.error('Товар не найден'))
      .finally(() => setLoading(false));
  }, [id]);

  const loadImages = useCallback(async () => {
    setImagesLoading(true);
    try {
      const res = await imageApi.list(Number(id));
      setImages(res.data.data);
    } catch {
      // ignore
    } finally {
      setImagesLoading(false);
    }
  }, [id]);

  useEffect(() => {
    loadImages();
  }, [loadImages]);

  // Polling статуса поиска каждые 2с
  useEffect(() => {
    if (!searchTaskId) return;
    const interval = setInterval(async () => {
      try {
        const res = await imageApi.searchStatus(Number(id), searchTaskId);
        const state: string = res.data.data.state;
        if (state !== 'running') {
          setSearchTaskId(null);
          if (state === 'done') {
            toast.success('Поиск завершён');
            loadImages();
          } else {
            toast.error('Поиск завершился с ошибкой');
          }
        }
      } catch {
        setSearchTaskId(null);
        toast.error('Ошибка при опросе статуса');
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [searchTaskId, id, loadImages]);

  async function runAction(action: 'publish' | 'archive' | 'regenerate') {
    setActionLoading(action);
    try {
      if (action === 'publish') await productApi.publish(Number(id));
      else if (action === 'archive') await productApi.archive(Number(id));
      else await productApi.regenerate(Number(id));
      toast.success(
        action === 'publish'
          ? 'Задача на публикацию поставлена'
          : action === 'archive'
            ? 'Товар архивируется'
            : 'Генерация описания запущена',
      );
    } catch {
      toast.error('Ошибка выполнения действия');
    } finally {
      setActionLoading(null);
    }
  }

  async function startSearch() {
    setActionLoading('search');
    try {
      const res = await imageApi.search(Number(id));
      setSearchTaskId(res.data.data.task_id);
      toast.info('Поиск фотографий запущен');
    } catch {
      toast.error('Не удалось запустить поиск');
    } finally {
      setActionLoading(null);
    }
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = '';
    setActionLoading('upload');
    try {
      await imageApi.upload(Number(id), file);
      toast.success('Фото загружено');
      loadImages();
    } catch {
      toast.error('Ошибка загрузки фото');
    } finally {
      setActionLoading(null);
    }
  }

  async function handleImageAction(
    imageId: number,
    action: 'approve' | 'reject' | 'setPrimary' | 'delete',
  ) {
    setImageActionId(imageId);
    try {
      if (action === 'approve') await imageApi.approve(Number(id), imageId);
      else if (action === 'reject') await imageApi.reject(Number(id), imageId);
      else if (action === 'setPrimary') await imageApi.setPrimary(Number(id), imageId);
      else await imageApi.delete(Number(id), imageId);
      loadImages();
    } catch {
      toast.error('Ошибка');
    } finally {
      setImageActionId(null);
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <div className="grid gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2 space-y-4">
            <Skeleton className="h-48 w-full rounded-xl" />
            <Skeleton className="h-32 w-full rounded-xl" />
          </div>
          <Skeleton className="h-64 w-full rounded-xl" />
        </div>
      </div>
    );
  }

  if (!product) {
    return (
      <div className="flex flex-col items-center gap-4 py-24 text-center">
        <Package className="h-12 w-12 text-muted-foreground/30" />
        <p className="text-muted-foreground">Товар не найден</p>
        <Button variant="outline" onClick={() => router.back()}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>
      </div>
    );
  }

  const busy = actionLoading !== null;

  return (
    <div className="space-y-6">
      {/* Навигация */}
      <div className="flex items-center gap-3">
        <Link href="/dashboard/products">
          <Button variant="ghost" size="sm">
            <ArrowLeft className="mr-2 h-4 w-4" />
            Каталог
          </Button>
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="font-mono text-sm">{product.article}</span>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Основная информация + Фото */}
        <div className="space-y-6 lg:col-span-2">
          <Card>
            <CardHeader className="flex flex-row items-start justify-between">
              <div>
                <CardTitle className="text-xl">{product.name}</CardTitle>
                <p className="mt-1 font-mono text-sm text-muted-foreground">{product.article}</p>
              </div>
              <Badge variant={product.export_enabled ? 'default' : 'secondary'}>
                {product.export_enabled ? 'Выгружается' : 'Не выгружается'}
              </Badge>
            </CardHeader>
            <CardContent>
              <Separator className="mb-4" />
              <div className="divide-y">
                <Field label="Бренд" value={product.brand} />
                <Field label="Категория 1С" value={product.category_1c} />
                <Field label="Состояние" value={CONDITION_LABELS[product.condition] ?? product.condition} />
                <Field label="Склад" value={product.warehouse} />
                <Field
                  label="Цена"
                  value={
                    <span className="text-lg font-bold">
                      {Number(product.price).toLocaleString('ru-RU')} ₽
                    </span>
                  }
                />
                <Field
                  label="Остаток"
                  value={
                    <span className={product.stock_qty === 0 ? 'text-destructive' : ''}>
                      {product.stock_qty} шт.
                    </span>
                  }
                />
                <Field
                  label="Последняя синхронизация"
                  value={
                    product.sync_at
                      ? new Date(product.sync_at).toLocaleString('ru-RU')
                      : '—'
                  }
                />
              </div>
            </CardContent>
          </Card>

          {/* Фотографии */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">
                Фотографии {images.length > 0 && `(${images.length})`}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {imagesLoading ? (
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                  {Array.from({ length: 4 }).map((_, i) => (
                    <Skeleton key={i} className="aspect-square rounded-lg" />
                  ))}
                </div>
              ) : images.length === 0 ? (
                <div className="flex flex-col items-center gap-2 py-8 text-center text-muted-foreground">
                  <ImageOff className="h-8 w-8 opacity-30" />
                  <p className="text-sm">Фотографии не загружены</p>
                  {searching && (
                    <p className="text-xs flex items-center gap-1">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      Идёт поиск...
                    </p>
                  )}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  {images
                    .sort((a, b) => a.position - b.position)
                    .map((img) => (
                      <div key={img.id} className="space-y-1">
                        <div className="relative aspect-square overflow-hidden rounded-lg border bg-muted">
                          {img.is_primary && (
                            <div className="absolute top-1 left-1 z-10">
                              <Crown className="h-4 w-4 text-yellow-500 drop-shadow" />
                            </div>
                          )}
                          <button
                            type="button"
                            onClick={() => setPreviewImg(img.url || img.thumb_url)}
                            className="w-full h-full"
                          >
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={img.thumb_url || img.url_source || ''}
                              alt=""
                              className="h-full w-full object-cover"
                              loading="lazy"
                            />
                          </button>
                        </div>
                        <Badge
                          variant={IMAGE_STATUS_VARIANTS[img.status] ?? 'outline'}
                          className="text-xs w-full justify-center"
                        >
                          {IMAGE_STATUS_LABELS[img.status] ?? img.status}
                        </Badge>
                        <div className="flex gap-1">
                          {!img.is_primary && (
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 flex-1"
                              title="Сделать главным"
                              disabled={imageActionId === img.id}
                              onClick={() => handleImageAction(img.id, 'setPrimary')}
                            >
                              <Crown className="h-3.5 w-3.5" />
                            </Button>
                          )}
                          {(img.status === 'needs_review' || img.status === 'low_confidence') && (
                            <>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 flex-1 text-green-600 hover:text-green-700"
                                title="Одобрить"
                                disabled={imageActionId === img.id}
                                onClick={() => handleImageAction(img.id, 'approve')}
                              >
                                <Check className="h-3.5 w-3.5" />
                              </Button>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 flex-1 text-destructive hover:text-destructive"
                                title="Отклонить"
                                disabled={imageActionId === img.id}
                                onClick={() => handleImageAction(img.id, 'reject')}
                              >
                                <X className="h-3.5 w-3.5" />
                              </Button>
                            </>
                          )}
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-7 w-7 flex-1 text-destructive hover:text-destructive"
                            title="Удалить"
                            disabled={imageActionId === img.id}
                            onClick={() => handleImageAction(img.id, 'delete')}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Действия */}
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Действия</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <Button
                className="w-full"
                onClick={() => runAction('publish')}
                disabled={busy || searching}
              >
                {actionLoading === 'publish' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="mr-2 h-4 w-4" />
                )}
                Опубликовать
              </Button>
              <Button
                className="w-full"
                variant="outline"
                onClick={() => runAction('regenerate')}
                disabled={busy || searching}
              >
                {actionLoading === 'regenerate' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="mr-2 h-4 w-4" />
                )}
                Сгенерировать описание
              </Button>
              <Button
                className="w-full"
                variant="outline"
                onClick={startSearch}
                disabled={busy || searching}
              >
                {searching || actionLoading === 'search' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Search className="mr-2 h-4 w-4" />
                )}
                {searching ? 'Поиск фото...' : 'Найти фото'}
              </Button>
              <Button
                className="w-full"
                variant="outline"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy || searching}
              >
                {actionLoading === 'upload' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="mr-2 h-4 w-4" />
                )}
                Загрузить фото
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={handleUpload}
              />
              <Button
                className="w-full"
                variant="outline"
                onClick={() => runAction('archive')}
                disabled={busy || searching}
              >
                {actionLoading === 'archive' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Archive className="mr-2 h-4 w-4" />
                )}
                Архивировать
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Информация</CardTitle>
            </CardHeader>
            <CardContent className="divide-y text-sm">
              <Field label="ID" value={product.id} />
              <Field
                label="Создан"
                value={new Date(product.created_at).toLocaleDateString('ru-RU')}
              />
              <Field
                label="Обновлён"
                value={new Date(product.updated_at).toLocaleDateString('ru-RU')}
              />
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Предпросмотр фото */}
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
    </div>
  );
}
