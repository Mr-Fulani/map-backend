'use client';

import { useEffect, useState } from 'react';
import { billingApi, imageApi, logApi } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { ListOrdered, Package, Sparkles, AlertTriangle, TrendingUp, XCircle, Image } from 'lucide-react';

interface UsageData {
  listings: { used: number; limit: number | null };
  sku: { used: number; limit: number | null };
  ai_credits: { used: number; limit: number | null };
  rejected_listings: number;
  subscription_status: string | null;
  grace_days_left: number | null;
  plan: string | null;
}

interface KpiCardProps {
  title: string;
  value: number;
  limit?: number | null;
  icon: React.ReactNode;
  loading: boolean;
  warning?: boolean;
}

function KpiCard({ title, value, limit, icon, loading, warning }: KpiCardProps) {
  const pct = limit ? Math.round((value / limit) * 100) : null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        <div className={warning ? 'text-destructive' : 'text-muted-foreground'}>{icon}</div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-8 w-24" />
        ) : (
          <>
            <div className="flex items-baseline gap-1">
              <span className="text-3xl font-bold">{value.toLocaleString('ru-RU')}</span>
              {limit && (
                <span className="text-sm text-muted-foreground">
                  / {limit.toLocaleString('ru-RU')}
                </span>
              )}
            </div>
            {pct !== null && (
              <>
                <div className="mt-2 h-1.5 w-full rounded-full bg-muted">
                  <div
                    className={`h-1.5 rounded-full transition-all ${
                      pct >= 90 ? 'bg-destructive' : pct >= 70 ? 'bg-yellow-500' : 'bg-primary'
                    }`}
                    style={{ width: `${Math.min(pct, 100)}%` }}
                  />
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{pct}% использовано</p>
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

const PLAN_LABELS: Record<string, string> = {
  starter: 'Starter',
  business: 'Business',
  pro: 'Pro',
  enterprise: 'Enterprise',
};

const STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive'> = {
  active: 'default',
  trial: 'secondary',
  past_due: 'destructive',
  cancelled: 'destructive',
};

const STATUS_LABELS: Record<string, string> = {
  active: 'Активна',
  trial: 'Пробный период',
  past_due: 'Триал истёк',
  cancelled: 'Отменена',
};

interface BraveQuota {
  remaining: number | null;
  limit: number | null;
}

export default function DashboardPage() {
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [errorsCount, setErrorsCount] = useState(0);
  const [braveQuota, setBraveQuota] = useState<BraveQuota>({ remaining: null, limit: null });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const today = new Date().toISOString().slice(0, 10);
        const [usageRes, logsRes, quotaRes] = await Promise.all([
          billingApi.getUsage(),
          logApi.list({ status: 'error', date: today }),
          imageApi.getQuota(),
        ]);
        setUsage(usageRes.data.data);
        setErrorsCount(logsRes.data.meta?.total ?? 0);
        setBraveQuota(quotaRes.data.data);
      } catch {
        // показываем нули вместо крэша
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const planLabel = usage?.plan ? (PLAN_LABELS[usage.plan] ?? usage.plan) : null;
  const subStatus = usage?.subscription_status ?? null;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight">Дашборд</h1>
          <p className="text-muted-foreground">Обзор платформы автоматизации маркетплейсов</p>
        </div>
        {planLabel && subStatus && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-muted-foreground">{planLabel}</span>
            <Badge variant={STATUS_VARIANTS[subStatus] ?? 'secondary'}>
              {STATUS_LABELS[subStatus] ?? subStatus}
            </Badge>
          </div>
        )}
      </div>

      {/* Баннер grace period */}
      {!loading && subStatus === 'past_due' && (
        <div className="flex flex-col gap-3 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <span className="font-semibold">Триал истёк.</span>{' '}
            {usage?.grace_days_left != null && usage.grace_days_left > 0
              ? `Публикация и AI заблокированы. До полного отключения осталось ${usage.grace_days_left} дн. — оплатите подписку.`
              : 'Подписка будет отменена. Оплатите подписку для восстановления доступа.'}
          </div>
          <a href="/dashboard/billing" className="shrink-0 font-semibold underline underline-offset-2">
            Оплатить
          </a>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <KpiCard
          title="Активные объявления"
          value={usage?.listings.used ?? 0}
          limit={usage?.listings.limit}
          icon={<ListOrdered className="h-4 w-4" />}
          loading={loading}
        />
        <KpiCard
          title="Товаров в каталоге"
          value={usage?.sku.used ?? 0}
          limit={usage?.sku.limit}
          icon={<Package className="h-4 w-4" />}
          loading={loading}
        />
        <KpiCard
          title="AI-кредиты"
          value={usage?.ai_credits.used ?? 0}
          limit={usage?.ai_credits.limit}
          icon={<Sparkles className="h-4 w-4" />}
          loading={loading}
        />
        <KpiCard
          title="Brave квота (фото)"
          value={braveQuota.remaining ?? 0}
          limit={braveQuota.limit}
          icon={<Image className="h-4 w-4" />}
          loading={loading}
          warning={(braveQuota.remaining ?? Infinity) <= 50}
        />
        <KpiCard
          title="Отклонено сейчас"
          value={usage?.rejected_listings ?? 0}
          icon={<XCircle className="h-4 w-4" />}
          loading={loading}
          warning={(usage?.rejected_listings ?? 0) > 0}
        />
        <KpiCard
          title="Ошибок за сегодня"
          value={errorsCount}
          icon={<AlertTriangle className="h-4 w-4" />}
          loading={loading}
          warning={errorsCount > 0}
        />
      </div>

      {!loading && (usage?.sku.used ?? 0) === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
            <TrendingUp className="h-10 w-10 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground">
              Данных ещё нет. Подключите источник данных и запустите первую синхронизацию.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
