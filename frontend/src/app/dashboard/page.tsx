'use client';

import { useEffect, useState } from 'react';
import { accountApi, billingApi, imageApi, logApi } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  AlertTriangle,
  Image as ImageIcon,
  ListOrdered,
  Package,
  Sparkles,
  TrendingUp,
  XCircle,
} from 'lucide-react';

interface UsageData {
  listings: { used: number; limit: number | null };
  sku: { used: number; limit: number | null };
  ai_credits: { used: number; limit: number | null };
  rejected_listings: number;
  subscription_status: string | null;
  current_period_days_left: number | null;
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
  past_due: 'Подписка истекла',
  cancelled: 'Отменена',
};

function formatTrialDays(days: number) {
  const lastTwoDigits = days % 100;
  const lastDigit = days % 10;
  const unit = lastTwoDigits >= 11 && lastTwoDigits <= 14
    ? 'дней'
    : lastDigit === 1
      ? 'день'
      : lastDigit >= 2 && lastDigit <= 4
        ? 'дня'
        : 'дней';

  return `${days} ${unit}`;
}

interface BraveQuota {
  used: number | null;
  soft_cap: number | null;
  is_paused: boolean | null;
}

interface AvitoWarning {
  accountName: string;
  message: string;
}

export default function DashboardPage() {
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [errorsCount, setErrorsCount] = useState(0);
  const [braveQuota, setBraveQuota] = useState<BraveQuota>({ used: null, soft_cap: null, is_paused: null });
  const [avitoWarnings, setAvitoWarnings] = useState<AvitoWarning[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const today = new Date().toISOString().slice(0, 10);
        const [usageRes, logsRes, quotaRes, accountsRes] = await Promise.all([
          billingApi.getUsage(),
          logApi.list({ status: 'error', date: today }),
          imageApi.getQuota(),
          accountApi.list(),
        ]);
        setUsage(usageRes.data.data);
        setErrorsCount(logsRes.data.meta?.total ?? 0);
        setBraveQuota(quotaRes.data.data);
        const accounts = accountsRes.data.data ?? accountsRes.data;
        const warnings: AvitoWarning[] = [];
        for (const account of accounts) {
          const health = account.avito_status;
          if (!health) continue;
          if (health.connection_status === 'auth_error') {
            warnings.push({ accountName: account.name, message: 'Avito отклонил ключи доступа' });
          } else if (health.autoload_status !== 'enabled' && health.autoload_status !== 'unknown') {
            warnings.push({ accountName: account.name, message: 'Автозагрузка не активирована' });
          } else if (health.tariff_status === 'inactive') {
            warnings.push({ accountName: account.name, message: 'Тариф Avito неактивен' });
          } else if (health.days_left !== null && health.days_left <= 7) {
            warnings.push({
              accountName: account.name,
              message: `до окончания тарифа осталось ${health.days_left} дн.`,
            });
          } else if (
            health.placements_remaining !== null
            && health.placements_total
            && health.placements_remaining / health.placements_total <= 0.2
          ) {
            warnings.push({
              accountName: account.name,
              message: `осталось ${health.placements_remaining} размещений из ${health.placements_total}`,
            });
          }
        }
        setAvitoWarnings(warnings);
      } catch {
        // показываем нули вместо крэша
      } finally {
        setLoading(false);
      }
    }

    async function refreshQuota() {
      try {
        const res = await imageApi.getQuota();
        setBraveQuota(res.data.data);
      } catch {
        // ignore
      }
    }

    load();
    const interval = setInterval(refreshQuota, 30_000);
    return () => clearInterval(interval);
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
              {(subStatus === 'trial' || subStatus === 'active')
                && usage?.current_period_days_left != null
                ? ` · осталось ${formatTrialDays(usage.current_period_days_left)}`
                : ''}
            </Badge>
          </div>
        )}
      </div>

      {/* Read-only сохраняет вход и оплату, но блокирует любые изменения данных. */}
      {!loading && (subStatus === 'past_due' || subStatus === 'cancelled') && (
        <div className="flex flex-col gap-3 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <span className="font-semibold">Подписка неактивна.</span>{' '}
            Просмотр данных доступен, изменения, импорт, публикация и AI заблокированы.
            {usage?.grace_days_left != null && usage.grace_days_left > 0
              ? ` До отмены подписки осталось ${usage.grace_days_left} дн.`
              : ''}{' '}
            Оплатите тариф для восстановления полного доступа.
          </div>
          <a href="/dashboard/billing" className="shrink-0 font-semibold underline underline-offset-2">
            Оплатить
          </a>
        </div>
      )}

      {!loading && avitoWarnings.length > 0 && (
        <div className="flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="font-semibold text-amber-700">Требуется внимание к Avito</p>
            <ul className="mt-1 space-y-0.5 text-muted-foreground">
              {avitoWarnings.slice(0, 3).map((warning) => (
                <li key={`${warning.accountName}-${warning.message}`}>
                  {warning.accountName}: {warning.message}
                </li>
              ))}
            </ul>
          </div>
          <a
            href="/dashboard/settings#marketplaces"
            className="shrink-0 font-semibold text-amber-700 underline underline-offset-2"
          >
            Открыть настройки
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
          title="Brave запросов (месяц)"
          value={braveQuota.used ?? 0}
          limit={braveQuota.soft_cap ?? 800}
          icon={<ImageIcon className="h-4 w-4" />}
          loading={loading}
          warning={braveQuota.is_paused ?? false}
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
