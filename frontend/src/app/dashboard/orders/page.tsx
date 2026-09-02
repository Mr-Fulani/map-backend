'use client';

import { useCallback, useEffect, useState } from 'react';
import { Loader2, RefreshCw, ShoppingBag } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { accountApi } from '@/lib/api';

interface Account { id: number; name: string; marketplace: string; is_active: boolean }
interface OrderProduct { offer_id: string; sku: string; name: string; quantity: number; price: string }
interface Order {
  id: number; posting_number: string; status: string; substatus: string;
  in_process_at: string | null; shipment_date: string | null; warehouse_id: string;
  products: OrderProduct[]; last_synced_at: string;
}

function date(value: string | null) {
  return value ? new Date(value).toLocaleString('ru-RU') : '—';
}

export default function OrdersPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    void accountApi.list().then((response) => {
      const next = (response.data as Account[]).filter((item) => item.marketplace === 'ozon' && item.is_active);
      setAccounts(next);
      setAccountId((current) => current ?? next[0]?.id ?? null);
    }).catch(() => toast.error('Не удалось загрузить кабинеты Ozon.'));
  }, []);

  const load = useCallback(async () => {
    if (!accountId) { setOrders([]); setLoading(false); return; }
    setLoading(true);
    try {
      const response = await accountApi.getOzonFbsOrders(accountId);
      setOrders(response.data.data as Order[]);
    } catch { toast.error('Не удалось загрузить FBS-заказы.'); }
    finally { setLoading(false); }
  }, [accountId]);

  useEffect(() => {
    if (!accountId) return;
    let cancelled = false;
    void accountApi.getOzonFbsOrders(accountId).then((response) => {
      if (!cancelled) setOrders(response.data.data as Order[]);
    }).catch(() => {
      if (!cancelled) toast.error('Не удалось загрузить FBS-заказы.');
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [accountId]);

  async function sync() {
    if (!accountId) return;
    setSyncing(true);
    try {
      await accountApi.syncOzonFbsOrders(accountId);
      await load();
      toast.success('Заказы Ozon обновлены.');
    } catch (error: unknown) {
      const message = (error as { response?: { data?: { message?: string } } }).response?.data?.message;
      toast.error(message ?? 'Не удалось обновить заказы Ozon.');
    } finally { setSyncing(false); }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="text-2xl font-semibold">Заказы</h1><p className="mt-1 text-sm text-muted-foreground">FBS-заказы хранятся отдельно для каждого кабинета Ozon.</p></div>
        <Button onClick={() => void sync()} disabled={!accountId || syncing}>
          {syncing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
          Обновить из Ozon
        </Button>
      </div>
      <div className="flex flex-wrap gap-2 rounded-lg border bg-card p-3">
        {accounts.map((account) => <Button key={account.id} variant={account.id === accountId ? 'default' : 'outline'} onClick={() => setAccountId(account.id)}>{account.name}</Button>)}
        {accounts.length === 0 && <p className="text-sm text-muted-foreground">Подключённого кабинета Ozon пока нет.</p>}
      </div>
      {loading ? <div className="flex justify-center p-10"><Loader2 className="h-6 w-6 animate-spin" /></div> : orders.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center text-sm text-muted-foreground"><ShoppingBag className="mx-auto mb-3 h-7 w-7" />Заказов за последние 14 дней пока нет.</div>
      ) : <div className="space-y-3">{orders.map((order) => (
        <article key={order.id} className="rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-start justify-between gap-2"><div><p className="font-mono text-sm font-medium">{order.posting_number}</p><p className="mt-1 text-xs text-muted-foreground">Создан: {date(order.in_process_at)} · Отгрузка: {date(order.shipment_date)}</p></div><Badge variant="outline">{order.status || 'Статус не указан'}</Badge></div>
          <div className="mt-3 space-y-2 border-t pt-3">{order.products.map((product, index) => <div key={`${product.offer_id}:${index}`} className="flex justify-between gap-3 text-sm"><span>{product.name || product.offer_id}<span className="ml-2 text-xs text-muted-foreground">{product.offer_id}</span></span><span className="shrink-0">{product.quantity} шт. · {product.price || '—'} ₽</span></div>)}</div>
        </article>
      ))}</div>}
    </div>
  );
}
