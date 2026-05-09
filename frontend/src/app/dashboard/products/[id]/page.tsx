'use client';

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import { productApi } from '@/lib/api';
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
} from 'lucide-react';

interface ProductImage {
  id: number;
  url_source: string;
  s3_key: string | null;
  position: number;
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
  images: ProductImage[];
  created_at: string;
  updated_at: string;
}

const CONDITION_LABELS: Record<string, string> = {
  new: 'Новый',
  used: 'Б/у',
  refurbished: 'Восстановленный',
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

  useEffect(() => {
    productApi
      .get(Number(id))
      .then((res) => setProduct(res.data.data))
      .catch(() => toast.error('Товар не найден'))
      .finally(() => setLoading(false));
  }, [id]);

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
            : 'Генерация описания запущена'
      );
    } catch {
      toast.error('Ошибка выполнения действия');
    } finally {
      setActionLoading(null);
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
        {/* Основная информация */}
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

          {/* Фото */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Фотографии</CardTitle>
            </CardHeader>
            <CardContent>
              {product.images.length === 0 ? (
                <div className="flex flex-col items-center gap-2 py-8 text-center text-muted-foreground">
                  <ImageOff className="h-8 w-8 opacity-30" />
                  <p className="text-sm">Фотографии не загружены</p>
                </div>
              ) : (
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5">
                  {product.images
                    .sort((a, b) => a.position - b.position)
                    .map((img) => (
                      <div
                        key={img.id}
                        className="aspect-square overflow-hidden rounded-lg border bg-muted"
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={img.url_source}
                          alt=""
                          className="h-full w-full object-cover"
                          loading="lazy"
                        />
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
                disabled={actionLoading !== null}
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
                disabled={actionLoading !== null}
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
                onClick={() => runAction('archive')}
                disabled={actionLoading !== null}
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
    </div>
  );
}
