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
  effective_status: string;
  access_mode: 'full' | 'billing_only';
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
  paid: 'Оплачен',
  failed: 'Ошибка',
};

const SUBSCRIPTION_STATUS: Record<string, string> = {
  trial: 'Пробный период',
  active: 'Активна',
  past_due: 'Истекла — доступ только для чтения',
  cancelled: 'Отменена — доступ только для чтения',
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
  const effectiveStatus = subscription
    ? (subscription.effective_status ?? subscription.status)
    : null;
  const hasFullAccess = subscription?.access_mode === 'full';

  return (
    <div className="space-y-6">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight">Биллинг</h1>
        <p className="text-muted-foreground">Управление подпиской и платежами</p>
      </div>

      {subscription && (
        <Card className={hasFullAccess ? '' : 'border-destructive/50'}>
          <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="font-semibold">
                {subscription.plan.name} · {
                  effectiveStatus
                    ? (SUBSCRIPTION_STATUS[effectiveStatus] ?? effectiveStatus)
                    : 'Статус неизвестен'
                }
              </p>
              <p className="text-sm text-muted-foreground">
                Текущий период до{' '}
                {subscription.current_period_end
                  ? new Date(`${subscription.current_period_end}T00:00:00`).toLocaleDateString('ru-RU')
                  : '—'}
              </p>
            </div>
            <Badge variant={hasFullAccess ? 'default' : 'destructive'}>
              {hasFullAccess ? 'Полный доступ' : 'Только чтение и оплата'}
            </Badge>
          </CardContent>
        </Card>
      )}

      {/* Тарифные планы */}
      <div>
        <h2 className="mb-3 text-lg font-semibold">Тарифные планы</h2>
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {plans.map((plan) => {
            const isCurrent = plan.slug === currentPlanSlug;
            const canRenewCurrent = isCurrent && !hasFullAccess;
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
                      disabled={(isCurrent && !canRenewCurrent) || checkoutLoading !== null}
                      onClick={() => checkout(plan.slug, 'monthly')}
                    >
                      {checkoutLoading === `${plan.slug}-monthly`
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : canRenewCurrent ? 'Продлить (мес)' : isCurrent ? 'Текущий план' : 'Выбрать (мес)'}
                    </Button>
                    {(!isCurrent || canRenewCurrent) && (
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
              <>
              <div className="grid gap-3 p-3 md:hidden">
                {invoices.map((inv) => (
                  <div key={inv.id} className="rounded-lg border p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-sm font-medium">
                          {Number(inv.amount).toLocaleString('ru-RU')} ₽
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {new Date(inv.created_at).toLocaleDateString('ru-RU')}
                        </p>
                      </div>
                      <Badge variant={inv.status === 'paid' ? 'default' : 'secondary'}>
                        {INVOICE_STATUS[inv.status] ?? inv.status}
                      </Badge>
                    </div>
                  </div>
                ))}
              </div>
              <div className="hidden overflow-x-auto md:block">
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
                        <Badge variant={inv.status === 'paid' ? 'default' : 'secondary'}>
                          {INVOICE_STATUS[inv.status] ?? inv.status}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
