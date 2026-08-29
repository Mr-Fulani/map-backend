'use client';

import { useEffect, useState } from 'react';
import { billingApi } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from 'sonner';
import { Loader2, CheckCircle2 } from 'lucide-react';
import {
  canStartBillingMutation,
  loadBillingPageData,
  type BillingLoadState,
} from '@/lib/billing-page-loader';
import { getListingLimitPresentation } from '@/lib/billing-plan-presentation';

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
  refunded_amount: string;
  refund_review_required: boolean;
  created_at: string;
}

interface AIUsage {
  ai_credits: {
    used: string;
    limit: string;
    included_balance: string;
    included_percent_used: string;
    purchased_balance: string;
    reserved_balance: string;
    available_balance: string;
    unlimited: boolean;
    individual_limit: boolean;
    overage_active: boolean;
    threshold: 'normal' | 'warning' | 'critical' | 'exhausted';
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
  partially_refunded: 'Частичный возврат',
  refunded: 'Возвращён',
  manual_review: 'Ручная проверка',
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

function billingErrorMessage(error: unknown, fallback: string): string {
  const payload = (error as {
    response?: { data?: { code?: string; message?: string } };
  })?.response?.data;
  if (payload?.code === 'checkout_pending') {
    return 'Платёж ещё создаётся. Повторите через несколько секунд — дубликат не появится.';
  }
  if (payload?.code === 'checkout_manual_review') {
    return 'Платёж требует ручной проверки. Новый платёж создавать не нужно.';
  }
  if (payload?.code === 'idempotency_conflict') {
    return 'Параметры платёжной попытки не совпадают. Не создавайте новый платёж — обратитесь в поддержку.';
  }
  if (payload?.code === 'checkout_key_limit') {
    return 'Лимит ключей этой платёжной попытки исчерпан. Не повторяйте оплату — обратитесь в поддержку.';
  }
  if (payload?.code === 'active_subscription_change_not_supported') {
    return 'Новый тариф можно оплатить после завершения текущего оплаченного периода.';
  }
  if (payload?.code === 'subscription_checkout_in_progress') {
    return 'Другой платёж подписки ещё не завершён. Дождитесь его статуса; новый платёж не создавайте.';
  }
  if (payload?.code === 'checkout_terminal') {
    return 'Предыдущая попытка уже завершена. Нажмите ещё раз, чтобы создать новый платёж.';
  }
  return payload?.message || fallback;
}

function navigateToPayment(value: unknown) {
  if (typeof value !== 'string') throw new Error('Платёжная ссылка отсутствует');
  const target = new URL(value);
  if (target.protocol !== 'https:' || target.username || target.password) {
    throw new Error('Платёжная ссылка должна использовать HTTPS');
  }
  window.location.assign(target.toString());
}

export default function BillingPage() {
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [billingEnabled, setBillingEnabled] = useState(false);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [usage, setUsage] = useState<AIUsage | null>(null);
  const [aiPackages, setAIPackages] = useState<AICreditPackage[]>([]);
  const [loading, setLoading] = useState(true);
  const [subscriptionState, setSubscriptionState] = useState<BillingLoadState>('loading');
  const [plansError, setPlansError] = useState(false);
  const [plansRetrying, setPlansRetrying] = useState(false);
  const [invoicesState, setInvoicesState] = useState<BillingLoadState>('loading');
  const [usageState, setUsageState] = useState<BillingLoadState>('loading');
  const [packagesState, setPackagesState] = useState<BillingLoadState>('loading');
  const [checkoutLoading, setCheckoutLoading] = useState<string | null>(null);
  const [topupLoading, setTopupLoading] = useState<number | null>(null);

  useEffect(() => {
    let active = true;
    const load = loadBillingPageData({
      subscription: () => billingApi.getSubscription(),
      plans: () => billingApi.getPlans(),
      invoices: () => billingApi.getInvoices(),
      usage: () => billingApi.getUsage(),
      packages: () => billingApi.getAIPackages(),
    });

    void load.subscription.then((result) => {
      if (!active) return;
      if (result.status === 'fulfilled') {
        setSubscription(result.value.data.data);
        setSubscriptionState('loaded');
      } else {
        setSubscriptionState('error');
        toast.error('Не удалось загрузить текущую подписку');
      }
    });

    void load.plans.then((result) => {
      if (!active) return;
      if (result.status === 'fulfilled') {
        setPlans(result.value.data.data);
        setBillingEnabled(result.value.data.billing_enabled === true);
      } else {
        setPlansError(true);
        toast.error('Не удалось загрузить тарифные планы');
      }
      setLoading(false);
    });

    void load.invoices.then((result) => {
      if (!active) return;
      if (result.status === 'fulfilled') {
        setInvoices(result.value.data.data);
        setInvoicesState('loaded');
      } else {
        setInvoicesState('error');
      }
    });

    void load.usage.then((result) => {
      if (!active) return;
      if (result.status === 'fulfilled') {
        setUsage(result.value.data.data);
        setUsageState('loaded');
      } else {
        setUsageState('error');
      }
    });

    void load.packages.then((result) => {
      if (!active) return;
      if (result.status === 'fulfilled') {
        setAIPackages(result.value.data.data);
        setPackagesState('loaded');
      } else {
        setPackagesState('error');
      }
    });

    return () => {
      active = false;
    };
  }, []);

  async function retrySubscription() {
    setSubscriptionState('loading');
    try {
      const response = await billingApi.getSubscription();
      setSubscription(response.data.data);
      setSubscriptionState('loaded');
    } catch {
      setSubscriptionState('error');
      toast.error('Не удалось загрузить текущую подписку');
    }
  }

  async function retryPlans() {
    setPlansRetrying(true);
    try {
      const response = await billingApi.getPlans();
      setPlans(response.data.data);
      setBillingEnabled(response.data.billing_enabled === true);
      setPlansError(false);
    } catch {
      setPlansError(true);
      toast.error('Не удалось загрузить тарифные планы');
    } finally {
      setPlansRetrying(false);
    }
  }

  async function checkout(planSlug: string, period: 'monthly' | 'yearly') {
    setCheckoutLoading(`${planSlug}-${period}`);
    try {
      const res = await billingApi.checkout(planSlug, period);
      navigateToPayment(res.data.data?.payment_url);
    } catch (error: unknown) {
      toast.error(billingErrorMessage(error, 'Ошибка создания платежа'));
    } finally {
      setCheckoutLoading(null);
    }
  }

  async function topupAI(packageId: number) {
    setTopupLoading(packageId);
    try {
      const res = await billingApi.topupAI(packageId);
      navigateToPayment(res.data.data?.payment_url);
    } catch (error: unknown) {
      toast.error(billingErrorMessage(error, 'Не удалось создать платёж на пополнение'));
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
  const billingMutationAllowed = canStartBillingMutation(
    subscriptionState,
    billingEnabled,
  );
  const subscriptionCheckoutAllowed = billingMutationAllowed && !hasFullAccess;
  const aiPercentUsed = Math.min(
    100,
    Number(usage?.ai_credits.included_percent_used ?? 0),
  );

  return (
    <div className="space-y-6">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight">Биллинг</h1>
        <p className="text-muted-foreground">Управление подпиской и платежами</p>
      </div>

      {!loading && !billingEnabled && !plansError && (
        <Card className="border-amber-500/50">
          <CardContent className="p-4 text-sm sm:p-4">
            Онлайн-оплата временно недоступна. Текущий тариф, лимиты и история
            платежей остаются доступны.
          </CardContent>
        </Card>
      )}

      {subscriptionState === 'loading' && (
        <Card>
          <CardContent className="p-4 sm:p-4">
            <Skeleton className="h-12 w-full" />
          </CardContent>
        </Card>
      )}

      {subscriptionState === 'error' && (
        <Card className="border-destructive/50">
          <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-4">
            <p className="text-sm text-destructive">
              Статус подписки недоступен. Оплата временно заблокирована, чтобы исключить повторное списание.
            </p>
            <Button variant="outline" size="sm" onClick={() => void retrySubscription()}>
              Повторить проверку
            </Button>
          </CardContent>
        </Card>
      )}

      {subscriptionState === 'loaded' && subscription && (
        <Card className={hasFullAccess ? '' : 'border-destructive/50'}>
          <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-4">
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
        {hasFullAccess && (
          <p className="mb-3 text-sm text-muted-foreground">
            Новый тариф можно оплатить после завершения текущего оплаченного периода.
          </p>
        )}
        {plansError && (
          <Card className="border-destructive/50">
            <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-4">
              <p className="text-sm text-destructive">
                Тарифные планы временно недоступны.
              </p>
              <Button
                variant="outline"
                size="sm"
                disabled={plansRetrying}
                onClick={() => void retryPlans()}
              >
                {plansRetrying ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Повторить'}
              </Button>
            </CardContent>
          </Card>
        )}
        {!plansError && plans.length === 0 && (
          <Card>
            <CardContent className="p-4 text-sm text-muted-foreground sm:p-4">
              Доступных тарифных планов сейчас нет.
            </CardContent>
          </Card>
        )}
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {plans.map((plan) => {
            const isCurrent = plan.slug === currentPlanSlug;
            const canRenewCurrent = isCurrent && !hasFullAccess;
            const listingLimits = getListingLimitPresentation(plan.limit_listings);
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
                    <p>• {listingLimits.total}</p>
                    <p>• {listingLimits.perAvitoAccount}</p>
                    <p>• {fmt(plan.limit_sku)} SKU</p>
                    <p>• {fmt(plan.limit_ai_credits)} AI-кредитов</p>
                    {listingLimits.requiresMultipleAvitoAccounts && (
                      <p className="pt-1 text-xs">
                        Объём свыше 10 000 распределяется между несколькими Avito-аккаунтами.
                      </p>
                    )}
                  </div>
                  <Separator />
                  <div className="space-y-2">
                    <Button
                      className="w-full"
                      size="sm"
                      variant={isCurrent ? 'outline' : 'default'}
                      disabled={
                        !subscriptionCheckoutAllowed
                        || (isCurrent && !canRenewCurrent)
                        || checkoutLoading !== null
                      }
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
                          disabled={!subscriptionCheckoutAllowed || checkoutLoading !== null}
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
            {usageState === 'loading' ? (
              <Skeleton className="h-32 w-full rounded-lg" />
            ) : usageState === 'error' ? (
              <p className="text-sm text-destructive">
                Баланс AI-кредитов временно недоступен.
              </p>
            ) : <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span>
                  Использовано{' '}
                  {Number(usage?.ai_credits.used ?? 0).toLocaleString('ru-RU')} из{' '}
                  {Number(usage?.ai_credits.limit ?? 0).toLocaleString('ru-RU')}
                </span>
                <div className="flex gap-2">
                  {usage?.ai_credits.individual_limit && (
                    <Badge variant="outline">Индивидуальный лимит</Badge>
                  )}
                  {usage?.ai_credits.overage_active && (
                    <Badge variant="destructive">Расходуется купленный баланс</Badge>
                  )}
                  {usage?.ai_credits.threshold === 'warning' && (
                    <Badge variant="secondary">Использовано 80%+</Badge>
                  )}
                  {usage?.ai_credits.threshold === 'critical' && (
                    <Badge variant="destructive">Использовано 90%+</Badge>
                  )}
                  {usage?.ai_credits.threshold === 'exhausted' && (
                    <Badge variant="destructive">Пакет исчерпан</Badge>
                  )}
                </div>
              </div>
              <Progress value={aiPercentUsed} />
              <p className="text-xs text-muted-foreground">
                Уведомления отправляются при достижении 80%, 90% и 100%.
              </p>
            </div>}

            {usageState === 'loaded' && <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border p-4">
                <p className="text-xs text-muted-foreground">Доступно</p>
                <p className="mt-1 text-2xl font-bold">
                  {Number(usage?.ai_credits.available_balance ?? 0).toLocaleString('ru-RU')}
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
            </div>}

            <div>
              <p className="mb-3 text-sm font-medium">Пополнить баланс</p>
              {packagesState === 'loading' ? (
                <Skeleton className="h-32 w-full rounded-lg" />
              ) : packagesState === 'error' ? (
                <p className="text-sm text-destructive">
                  Пакеты пополнения временно недоступны.
                </p>
              ) : aiPackages.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Доступных пакетов пополнения сейчас нет.
                </p>
              ) : <div className="grid gap-3 md:grid-cols-3">
                {aiPackages.map((item) => (
                  <div key={item.id} className="rounded-lg border p-4">
                    <p className="font-semibold">{item.name}</p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {Number(item.credits).toLocaleString('ru-RU')} кредитов
                    </p>
                    <Button
                      className="mt-4 w-full"
                      size="sm"
                      disabled={!billingMutationAllowed || topupLoading !== null}
                      onClick={() => topupAI(item.id)}
                    >
                      {topupLoading === item.id
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : `${Number(item.price_rub).toLocaleString('ru-RU')} ₽`}
                    </Button>
                  </div>
                ))}
              </div>}
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
          <CardContent className="p-0 sm:p-0">
            {invoicesState === 'loading' ? (
              <div className="space-y-2 p-4">
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
              </div>
            ) : invoicesState === 'error' ? (
              <p className="py-8 text-center text-sm text-destructive">
                История платежей временно недоступна.
              </p>
            ) : invoices.length === 0 ? (
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
                        {Number(inv.refunded_amount) > 0 && (
                          <p className="text-xs text-muted-foreground">
                            Возвращено: {Number(inv.refunded_amount).toLocaleString('ru-RU')} ₽
                          </p>
                        )}
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
                        {Number(inv.refunded_amount) > 0 && (
                          <span className="block text-xs font-normal text-muted-foreground">
                            Возвращено {Number(inv.refunded_amount).toLocaleString('ru-RU')} ₽
                          </span>
                        )}
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
