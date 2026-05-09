'use client';

import { useState } from 'react';
import { listingApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { CheckCircle, RefreshCw, Pencil } from 'lucide-react';

interface ListingImage {
  url: string;
  thumb_url: string;
  position: number;
}

interface ListingDetail {
  id: number;
  status: string;
  status_display: string;
  product_article: string;
  product_name: string;
  account_name: string;
  title: string;
  description_ai: string;
  ai_confidence: number | null;
  ai_confidence_display: string;
  price_on_listing: string;
  rejection_reason: string;
  images: ListingImage[];
}

interface Props {
  listingId: number | null;
  onClose: () => void;
  onActionDone: () => void;
}

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  pending: 'secondary',
  draft: 'outline',
  rejected: 'destructive',
  requires_review: 'destructive',
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
  const [previewImg, setPreviewImg] = useState<string | null>(null);

  const open = listingId !== null;

  // Загружаем данные при открытии дровера
  const handleOpenChange = async (isOpen: boolean) => {
    if (!isOpen) {
      onClose();
      setListing(null);
      setEditing(false);
      return;
    }
    if (listingId === null) return;
    setLoading(true);
    try {
      const res = await listingApi.get(listingId);
      const data: ListingDetail = res.data.data;
      setListing(data);
      setEditTitle(data.title);
      setEditDesc(data.description_ai);
    } catch {
      onClose();
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async () => {
    if (!listing) return;
    setActionLoading('approve');
    try {
      await listingApi.approve(listing.id);
      onActionDone();
      onClose();
    } finally {
      setActionLoading(null);
    }
  };

  const handleRegenerate = async () => {
    if (!listing) return;
    setActionLoading('regenerate');
    try {
      await listingApi.regenerate(listing.id);
      onActionDone();
      onClose();
    } finally {
      setActionLoading(null);
    }
  };

  const handleSaveEdit = async () => {
    if (!listing) return;
    setActionLoading('save');
    try {
      const res = await listingApi.updateContent(listing.id, {
        title: editTitle,
        description_ai: editDesc,
      });
      setListing(res.data.data);
      setEditing(false);
    } finally {
      setActionLoading(null);
    }
  };

  const isReview = listing?.status === 'requires_review';
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

              {/* Уверенность AI */}
              <ConfidenceBar value={listing.ai_confidence} label={listing.ai_confidence_display} />

              {/* Заголовок объявления */}
              <div className="space-y-1">
                <p className="text-sm text-muted-foreground">Заголовок</p>
                {editing ? (
                  <Input
                    value={editTitle}
                    maxLength={300}
                    onChange={(e) => setEditTitle(e.target.value)}
                  />
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
                  <pre className="whitespace-pre-wrap text-sm bg-muted/40 rounded-md p-3 font-sans leading-relaxed max-h-64 overflow-y-auto">
                    {listing.description_ai || '—'}
                  </pre>
                )}
              </div>

              {/* Фотографии */}
              {listing.images.length > 0 && (
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">Фотографии ({listing.images.length})</p>
                  <div className="flex gap-2 overflow-x-auto pb-1">
                    {listing.images.map((img) => (
                      <button
                        key={img.position}
                        type="button"
                        onClick={() => setPreviewImg(img.url)}
                        className="flex-shrink-0 w-16 h-16 rounded-md overflow-hidden border bg-muted hover:opacity-80 transition-opacity"
                      >
                        {img.thumb_url ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={img.thumb_url} alt="" className="w-full h-full object-cover" />
                        ) : (
                          <div className="w-full h-full flex items-center justify-center text-xs text-muted-foreground">
                            Фото
                          </div>
                        )}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Цена и аккаунт */}
              <div className="flex items-center justify-between rounded-md border p-3">
                <span className="text-muted-foreground text-sm">{listing.account_name}</span>
                <span className="text-xl font-bold">
                  {Number(listing.price_on_listing).toLocaleString('ru-RU')} ₽
                </span>
              </div>

              {/* Причина отклонения (если есть) */}
              {listing.rejection_reason && (
                <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                  <span className="font-medium">Причина отклонения: </span>
                  {listing.rejection_reason}
                </div>
              )}

              {/* Кнопки действий */}
              <div className="flex flex-col gap-2 pt-2">
                {editing ? (
                  <>
                    <Button
                      onClick={handleSaveEdit}
                      disabled={busy}
                      className="w-full"
                    >
                      {actionLoading === 'save' ? 'Сохранение...' : 'Сохранить изменения'}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setEditing(false);
                        setEditTitle(listing.title);
                        setEditDesc(listing.description_ai);
                      }}
                      disabled={busy}
                      className="w-full"
                    >
                      Отмена
                    </Button>
                  </>
                ) : (
                  <>
                    {isReview && (
                      <>
                        <Button
                          onClick={handleApprove}
                          disabled={busy}
                          className="w-full bg-green-600 hover:bg-green-700 text-white"
                        >
                          <CheckCircle className="mr-2 h-4 w-4" />
                          {actionLoading === 'approve' ? 'Публикация...' : 'Одобрить и опубликовать'}
                        </Button>
                        <Button
                          variant="secondary"
                          onClick={handleRegenerate}
                          disabled={busy}
                          className="w-full"
                        >
                          <RefreshCw className="mr-2 h-4 w-4" />
                          {actionLoading === 'regenerate' ? 'Отправка задачи...' : 'Перегенерировать AI'}
                        </Button>
                      </>
                    )}
                    <Button
                      variant="outline"
                      onClick={() => setEditing(true)}
                      disabled={busy}
                      className="w-full"
                    >
                      <Pencil className="mr-2 h-4 w-4" />
                      Редактировать текст
                    </Button>
                  </>
                )}
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>

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
    </>
  );
}
