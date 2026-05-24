'use client';

import { useEffect, useState } from 'react';
import { billingApi } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from 'sonner';
import { Loader2, CheckCircle2 } from 'lucide-react';

interface Plan {
  id: number;
  name: string;
  slug: string;
  price_monthly: string;
  price_yearly: string;
  limit_listings: number | null;
  limit_sku: number | null;
  limit_ai_credits: number | null;
}

interface Subscription {
  id: number;
  plan: Plan;
  status: string;
  billing_period: string;
  current_period_start: string;
  current_period_end: string | null;
}

interface Invoice {
  id: number;
  amount: string;
  status: string;
  paid_at: string | null;
  created_at: string;
}

const INVOICE_STATUS: Record<string, string> = {
  pending: 'Ожидает',
  succeeded: 'Оплачен',
  cancelled: 'Отменён',
};

function fmt(n: number | null) {
  return n === null ? '∞' : n.toLocaleString('ru-RU');
}

export default function BillingPage() {
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [checkoutLoading, setCheckoutLoading] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      billingApi.getSubscription(),
      billingApi.getPlans(),
      billingApi.getInvoices(),
    ]).then(([subRes, plansRes, invRes]) => {
      setSubscription(subRes.data.data);
      setPlans(plansRes.data.data);
      setInvoices(invRes.data.data);
    }).catch(() => {
      toast.error('Не удалось загрузить данные биллинга');
    }).finally(() => setLoading(false));
  }, []);

  async function checkout(planSlug: string, period: 'monthly' | 'yearly') {
    setCheckoutLoading(`${planSlug}-${period}`);
    try {
      const res = await billingApi.checkout(planSlug, period);
      const url = res.data.data?.payment_url;
      if (url) window.open(url, '_blank');
    } catch {
      toast.error('Ошибка создания платежа');
    } finally {
      setCheckoutLoading(null);
    }
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-40 w-full rounded-xl" />
        <div className="grid gap-4 md:grid-cols-3">
          {[1, 2, 3].map((i) => <Skeleton key={i} className="h-64 rounded-xl" />)}
        </div>
      </div>
    );
  }

  const currentPlanSlug = subscription?.plan?.slug;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Биллинг</h1>
        <p className="text-muted-foreground">Управление подпиской и платежами</p>
      </div>

      {/* Тарифные планы */}
      <div>
        <h2 className="mb-3 text-lg font-semibold">Тарифные планы</h2>
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {plans.map((plan) => {
            const isCurrent = plan.slug === currentPlanSlug;
            return (
              <Card key={plan.id} className={isCurrent ? 'border-primary' : ''}>
                <CardHeader className="pb-3">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-base">{plan.name}</CardTitle>
                    {isCurrent && <CheckCircle2 className="h-4 w-4 text-primary" />}
                  </div>
                  <p className="text-2xl font-bold">
                    {Number(plan.price_monthly).toLocaleString('ru-RU')} ₽
                    <span className="text-sm font-normal text-muted-foreground">/мес</span>
                  </p>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="space-y-1 text-sm text-muted-foreground">
                    <p>• {fmt(plan.limit_listings)} объявлений</p>
                    <p>• {fmt(plan.limit_sku)} SKU</p>
                    <p>• {fmt(plan.limit_ai_credits)} AI-генераций</p>
                  </div>
                  <Separator />
                  <div className="space-y-2">
                    <Button
                      className="w-full"
                      size="sm"
                      variant={isCurrent ? 'outline' : 'default'}
                      disabled={isCurrent || checkoutLoading !== null}
                      onClick={() => checkout(plan.slug, 'monthly')}
                    >
                      {checkoutLoading === `${plan.slug}-monthly`
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : isCurrent ? 'Текущий план' : 'Выбрать (мес)'}
                    </Button>
                    {!isCurrent && (
                      <Button
                        className="w-full"
                        size="sm"
                        variant="outline"
                        disabled={checkoutLoading !== null}
                        onClick={() => checkout(plan.slug, 'yearly')}
                      >
                        {checkoutLoading === `${plan.slug}-yearly`
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : `Год (−20%) — ${Number(plan.price_yearly).toLocaleString('ru-RU')} ₽`}
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>

      {/* История платежей */}
      <div>
        <h2 className="mb-3 text-lg font-semibold">История платежей</h2>
        <Card>
          <CardContent className="p-0">
            {invoices.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">Платежей пока нет</p>
            ) : (
              <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50 text-left text-muted-foreground">
                    <th className="px-4 py-3 font-medium">Дата</th>
                    <th className="px-4 py-3 font-medium">Сумма</th>
                    <th className="px-4 py-3 font-medium">Статус</th>
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((inv) => (
                    <tr key={inv.id} className="border-b last:border-0">
                      <td className="px-4 py-3 text-muted-foreground">
                        {new Date(inv.created_at).toLocaleDateString('ru-RU')}
                      </td>
                      <td className="px-4 py-3 font-medium">
                        {Number(inv.amount).toLocaleString('ru-RU')} ₽
                      </td>
                      <td className="px-4 py-3">
                        <Badge variant={inv.status === 'succeeded' ? 'default' : 'secondary'}>
                          {INVOICE_STATUS[inv.status] ?? inv.status}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
