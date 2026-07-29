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
  price_yearly_monthly_equivalent: string;
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
  ai_period_start: string | null;
  ai_period_end: string | null;
}

interface Invoice {
  id: number;
  purchase_type: 'subscription' | 'ai_topup';
  amount: string;
  currency: string;
  status: string;
  paid_at: string | null;
  created_at: string;
}

interface AIUsage {
  ai_credits: {
    included_balance: string;
    purchased_balance: string;
    reserved_balance: string;
    available_balance: string;
    unlimited: boolean;
  };
}

interface AICreditPackage {
  id: number;
  name: string;
  credits: string;
  price_rub: string;
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

const INVOICE_TYPE: Record<string, string> = {
  subscription: 'Подписка',
  ai_topup: 'AI-кредиты',
};

function fmt(n: number | null) {
  return n === null ? '∞' : n.toLocaleString('ru-RU');
}

export default function BillingPage() {
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [usage, setUsage] = useState<AIUsage | null>(null);
  const [aiPackages, setAIPackages] = useState<AICreditPackage[]>([]);
  const [loading, setLoading] = useState(true);
  const [checkoutLoading, setCheckoutLoading] = useState<string | null>(null);
  const [topupLoading, setTopupLoading] = useState<number | null>(null);

  useEffect(() => {
    Promise.all([
      billingApi.getSubscription(),
      billingApi.getPlans(),
      billingApi.getInvoices(),
      billingApi.getUsage(),
      billingApi.getAIPackages(),
    ]).then(([subRes, plansRes, invRes, usageRes, packagesRes]) => {
      setSubscription(subRes.data.data);
      setPlans(plansRes.data.data);
      setInvoices(invRes.data.data);
      setUsage(usageRes.data.data);
      setAIPackages(packagesRes.data.data);
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

  async function topupAI(packageId: number) {
    setTopupLoading(packageId);
    try {
      const res = await billingApi.topupAI(packageId);
      const url = res.data.data?.payment_url;
      if (url) window.open(url, '_blank');
    } catch {
      toast.error('Не удалось создать платёж на пополнение');
    } finally {
      setTopupLoading(null);
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
                    <p>• {fmt(plan.limit_ai_credits)} AI-кредитов</p>
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
                      <div className="space-y-1">
                        <Button
                          className="w-full"
                          size="sm"
                          variant="outline"
                          disabled={checkoutLoading !== null}
                          onClick={() => checkout(plan.slug, 'yearly')}
                        >
                          {checkoutLoading === `${plan.slug}-yearly`
                            ? <Loader2 className="h-4 w-4 animate-spin" />
                            : `Оплатить год — ${Number(plan.price_yearly).toLocaleString('ru-RU')} ₽`}
                        </Button>
                        <p className="text-center text-xs text-muted-foreground">
                          {Number(plan.price_yearly_monthly_equivalent).toLocaleString('ru-RU')} ₽/мес
                          · скидка 20%
                        </p>
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>

      {/* AI-кредиты */}
      <div id="ai-credits" className="scroll-mt-6">
        <h2 className="mb-3 text-lg font-semibold">AI-кредиты</h2>
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Баланс</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border p-4">
                <p className="text-xs text-muted-foreground">Доступно</p>
                <p className="mt-1 text-2xl font-bold">
                  {usage?.ai_credits.unlimited
                    ? '∞'
                    : Number(usage?.ai_credits.available_balance ?? 0).toLocaleString('ru-RU')}
                </p>
              </div>
              <div className="rounded-lg border p-4">
                <p className="text-xs text-muted-foreground">Включено в тариф</p>
                <p className="mt-1 text-xl font-semibold">
                  {Number(usage?.ai_credits.included_balance ?? 0).toLocaleString('ru-RU')}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">Обновляется каждый период</p>
              </div>
              <div className="rounded-lg border p-4">
                <p className="text-xs text-muted-foreground">Куплено отдельно</p>
                <p className="mt-1 text-xl font-semibold">
                  {Number(usage?.ai_credits.purchased_balance ?? 0).toLocaleString('ru-RU')}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">Не сгорает при продлении</p>
              </div>
            </div>

            <div>
              <p className="mb-3 text-sm font-medium">Пополнить баланс</p>
              <div className="grid gap-3 md:grid-cols-3">
                {aiPackages.map((item) => (
                  <div key={item.id} className="rounded-lg border p-4">
                    <p className="font-semibold">{item.name}</p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {Number(item.credits).toLocaleString('ru-RU')} кредитов
                    </p>
                    <Button
                      className="mt-4 w-full"
                      size="sm"
                      disabled={topupLoading !== null}
                      onClick={() => topupAI(item.id)}
                    >
                      {topupLoading === item.id
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : `${Number(item.price_rub).toLocaleString('ru-RU')} ₽`}
                    </Button>
                  </div>
                ))}
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                Перед AI-запросом система резервирует ожидаемую стоимость, затем списывает
                фактическую по использованным токенам. При ошибке резерв возвращается.
              </p>
            </div>
          </CardContent>
        </Card>
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
                        <p className="text-xs text-muted-foreground">
                          {INVOICE_TYPE[inv.purchase_type] ?? inv.purchase_type}
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
                    <th className="px-4 py-3 font-medium">Назначение</th>
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
                      <td className="px-4 py-3">
                        {INVOICE_TYPE[inv.purchase_type] ?? inv.purchase_type}
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
