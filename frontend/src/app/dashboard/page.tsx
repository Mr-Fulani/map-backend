'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Coins,
  Database,
  Eye,
  ListChecks,
  ListOrdered,
  Loader2,
  Package,
  Phone,
  RefreshCw,
  Settings2,
  Sparkles,
  Store,
  Upload,
  WandSparkles,
  XCircle,
} from 'lucide-react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { dashboardApi } from '@/lib/api';
import {
  dashboardAIBalanceState,
  dashboardNumber,
  dashboardPercent,
  hiddenTasksLabel,
  safeDashboardHref,
  type DashboardActivityItem,
  type DashboardAttentionItem,
  type DashboardNumber,
  type DashboardSummary,
} from '@/lib/dashboard-summary';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

const PLAN_LABELS: Record<string, string> = {
  starter: 'Starter',
  business: 'Business',
  pro: 'Pro',
  enterprise: 'Enterprise',
};

const STATUS_LABELS: Record<string, string> = {
  active: 'Активна',
  trial: 'Пробный период',
  past_due: 'Подписка истекла',
  cancelled: 'Отменена',
};

const STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive'> = {
  active: 'default',
  trial: 'secondary',
  past_due: 'destructive',
  cancelled: 'destructive',
};

function formatNumber(value: DashboardNumber | null | undefined, digits = 0) {
  const normalized = dashboardNumber(value);
  if (normalized === null) return '—';
  return normalized.toLocaleString('ru-RU', {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return 'ещё не было';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'время неизвестно';
  return parsed.toLocaleString('ru-RU', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDay(value: string) {
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}

function formatDays(days: number) {
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

function SectionSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3" aria-label="Загрузка данных">
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className={cn('h-14 w-full', index === rows - 1 && 'w-4/5')} />
      ))}
    </div>
  );
}

function SectionUnavailable({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex min-h-28 flex-col items-center justify-center gap-3 rounded-lg border border-dashed p-5 text-center">
      <CircleAlert className="h-6 w-6 text-muted-foreground" />
      <p className="text-sm text-muted-foreground">Данные этого раздела временно недоступны.</p>
      <Button type="button" size="sm" variant="outline" onClick={onRetry}>
        <RefreshCw className="h-4 w-4" />
        Повторить
      </Button>
    </div>
  );
}

function AttentionIcon({ severity }: Pick<DashboardAttentionItem, 'severity'>) {
  if (severity === 'critical') return <XCircle className="h-5 w-5 text-destructive" />;
  if (severity === 'warning') return <AlertTriangle className="h-5 w-5 text-amber-600" />;
  return <CircleAlert className="h-5 w-5 text-blue-600" />;
}

function AttentionWidget({
  items,
  loading,
  onRetry,
  emptyTitle = 'Критичных задач нет',
  emptyDescription = 'Все основные процессы работают штатно.',
}: {
  items: DashboardAttentionItem[] | undefined;
  loading: boolean;
  onRetry: () => void;
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2 text-lg">
              <ListChecks className="h-5 w-5" />
              Требует внимания
            </CardTitle>
            <CardDescription className="mt-1">
              Приоритетные задачи, которые влияют на публикацию и продажи.
            </CardDescription>
          </div>
          {items && items.length > 0 && <Badge variant="secondary">{items.length}</Badge>}
        </div>
      </CardHeader>
      <CardContent>
        {loading ? <SectionSkeleton rows={3} /> : !items ? (
          <SectionUnavailable onRetry={onRetry} />
        ) : items.length === 0 ? (
          <div className="flex items-center gap-3 rounded-lg border border-green-500/20 bg-green-500/5 p-4">
            <CheckCircle2 className="h-6 w-6 shrink-0 text-green-600" />
            <div>
              <p className="font-medium">{emptyTitle}</p>
              <p className="text-sm text-muted-foreground">{emptyDescription}</p>
            </div>
          </div>
        ) : (
          <div className="divide-y rounded-lg border">
            {items.slice(0, 6).map((item, index) => {
              const href = safeDashboardHref(item.href);
              const content = (
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  <div className="mt-0.5 shrink-0"><AttentionIcon severity={item.severity} /></div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-medium">{item.title}</p>
                      {item.count !== null && item.count > 0 && (
                        <Badge variant={item.severity === 'critical' ? 'destructive' : 'secondary'}>
                          {item.count.toLocaleString('ru-RU')}
                        </Badge>
                      )}
                    </div>
                    <p className="mt-0.5 text-sm text-muted-foreground">{item.message}</p>
                  </div>
                  {href && <ArrowRight className="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />}
                </div>
              );
              return href ? (
                <Link
                  key={`${item.code}-${index}`}
                  href={href}
                  className="block p-3 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {content}
                </Link>
              ) : <div key={`${item.code}-${index}`} className="p-3">{content}</div>;
            })}
            {items.length > 6 && (
              <div className="bg-muted/30 p-3 text-sm">
                <span className="text-muted-foreground">{hiddenTasksLabel(items.length - 6)}</span>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PulseMetric({
  title,
  value,
  icon,
  suffix = '',
}: {
  title: string;
  value: DashboardNumber | null | undefined;
  icon: React.ReactNode;
  suffix?: string;
}) {
  return (
    <div className="rounded-lg border p-3">
      <div className="flex items-center justify-between gap-2 text-sm text-muted-foreground">
        <span>{title}</span>
        {icon}
      </div>
      <p className="mt-2 text-2xl font-bold">{formatNumber(value, suffix ? 1 : 0)}{suffix}</p>
    </div>
  );
}

function AnalyticsWidget({
  analytics,
  loading,
  onRetry,
}: {
  analytics: DashboardSummary['analytics'] | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  const hasChartData = analytics?.daily?.some((point) => (
    point.views > 0 || point.contacts > 0 || point.impressions > 0
  ));
  return (
    <Card className="min-w-0">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2 text-lg">
              <BarChart3 className="h-5 w-5" />
              Результат за 30 дней
            </CardTitle>
            <CardDescription className="mt-1">Уникальные и все просмотры, контакты по объявлениям.</CardDescription>
          </div>
          <Button asChild size="sm" variant="ghost">
            <Link href="/dashboard/analytics">Вся аналитика <ArrowRight className="h-4 w-4" /></Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? <SectionSkeleton rows={4} /> : !analytics ? (
          <SectionUnavailable onRetry={onRetry} />
        ) : (
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
              <PulseMetric title="Уникальные просмотры" value={analytics.summary?.views} icon={<Eye className="h-4 w-4" />} />
              <PulseMetric title="Контакты" value={analytics.summary?.contacts} icon={<Phone className="h-4 w-4" />} />
              <PulseMetric title="Все просмотры" value={analytics.summary?.impressions} icon={<BarChart3 className="h-4 w-4" />} />
              <PulseMetric title="Доля уникальных" value={analytics.summary?.avg_ctr} suffix="%" icon={<Activity className="h-4 w-4" />} />
            </div>
            <div
              className="h-56 min-w-0"
              role="img"
              aria-label="Динамика уникальных просмотров и контактов за 30 дней"
              aria-describedby="dashboard-chart-description"
            >
              {!hasChartData ? (
                <div className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed text-center text-muted-foreground">
                  <BarChart3 className="h-8 w-8 opacity-40" />
                  <p className="text-sm">Статистика появится после первых просмотров.</p>
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={analytics.daily} margin={{ top: 8, right: 8, left: -24, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={24} tickFormatter={formatDay} />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <ChartTooltip
                      labelFormatter={(value) => formatDay(String(value))}
                      formatter={(value, name) => [
                        Number(value).toLocaleString('ru-RU'),
                        name === 'views' ? 'Уникальные просмотры' : 'Контакты',
                      ]}
                    />
                    <Line type="monotone" dataKey="views" stroke="#6366f1" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="contacts" stroke="#22c55e" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </div>
            <p id="dashboard-chart-description" className="sr-only">
              Линейный график показывает ежедневное количество уникальных просмотров и контактов за последние 30 дней.
            </p>
            <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground" aria-label="Легенда графика">
              <span className="inline-flex items-center gap-2">
                <span className="h-0.5 w-5 bg-indigo-500" aria-hidden="true" /> Уникальные просмотры
              </span>
              <span className="inline-flex items-center gap-2">
                <span className="h-0.5 w-5 bg-green-500" aria-hidden="true" /> Контакты
              </span>
            </div>
            <div className="sr-only">
              <table>
                <caption>Ежедневные уникальные просмотры и контакты за последние 30 дней</caption>
                <thead><tr><th>Дата</th><th>Уникальные просмотры</th><th>Контакты</th></tr></thead>
                <tbody>
                  {analytics.daily.slice(0, 30).map((point) => (
                    <tr key={point.date}>
                      <td>{formatDay(point.date)}</td>
                      <td>{point.views.toLocaleString('ru-RU')}</td>
                      <td>{point.contacts.toLocaleString('ru-RU')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function FunnelWidget({
  funnel,
  loading,
  onRetry,
}: {
  funnel: DashboardSummary['funnel'] | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  const items = funnel ? [
    { label: 'Товары в каталоге', value: funnel.products, href: '/dashboard/products', icon: Package },
    { label: 'Всего объявлений', value: funnel.listings, href: '/dashboard/listings', icon: ListOrdered },
    { label: 'Активные в MAP', value: funnel.active_listings, href: '/dashboard/listings?status=active', icon: ListOrdered },
    { label: 'В очереди', value: funnel.queued_listings, href: '/dashboard/listings?status=queued', icon: Clock3 },
    { label: 'Отправка в Avito', value: funnel.pending_listings, href: '/dashboard/listings?status=pending', icon: Clock3 },
    { label: 'Требуют проверки', value: funnel.requires_review_listings, href: '/dashboard/listings?status=requires_review', icon: ListChecks },
    { label: 'Отклонены', value: funnel.rejected_listings, href: '/dashboard/listings?status=rejected', icon: XCircle },
    { label: 'Достигнут лимит Avito', value: funnel.limit_reached_listings, href: '/dashboard/listings?status=limit_reached', icon: AlertTriangle },
  ] : [];
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle role="heading" aria-level={2} className="flex items-center gap-2 text-lg">
          <ListOrdered className="h-5 w-5" />
          Каталог и публикация
        </CardTitle>
        <CardDescription>
          Счётчики MAP по всем аккаунтам. В списке активных объявлений показано,
          когда каждый статус проверен через Avito.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loading ? <SectionSkeleton rows={5} /> : !funnel ? (
          <SectionUnavailable onRetry={onRetry} />
        ) : (
          <div className="space-y-2">
            {items.map((item) => (
              <Link
                key={item.label}
                href={item.href}
                className="flex items-center gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <item.icon className="h-5 w-5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 text-sm">{item.label}</span>
                <span className="text-lg font-semibold">{formatNumber(item.value)}</span>
                <ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground" />
              </Link>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function sourceStatus(status: string, isActive: boolean) {
  if (!isActive) return { label: 'Отключён', className: 'text-muted-foreground', icon: Clock3 };
  if (status === 'ok' || status === 'success') return { label: 'Синхронизирован', className: 'text-green-600', icon: CheckCircle2 };
  if (status === 'error') return { label: 'Ошибка', className: 'text-destructive', icon: XCircle };
  return { label: 'Не синхронизирован', className: 'text-amber-600', icon: Clock3 };
}

function SourcesWidget({
  datasources,
  marketplaces,
  loading,
  onRetry,
}: {
  datasources: DashboardSummary['datasources'] | undefined;
  marketplaces: DashboardSummary['marketplaces'] | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2 text-lg">
              <Database className="h-5 w-5" />
              Источники и подключения
            </CardTitle>
            <CardDescription className="mt-1">Свежесть каталога и состояние Avito.</CardDescription>
          </div>
          <Button asChild size="sm" variant="ghost">
            <Link href="/dashboard/settings#datasources">Настроить <Settings2 className="h-4 w-4" /></Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? <SectionSkeleton rows={4} /> : !datasources || !marketplaces ? (
          <SectionUnavailable onRetry={onRetry} />
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              <div className="rounded-lg bg-muted/50 p-3"><p className="text-muted-foreground">Активны</p><p className="mt-1 text-xl font-semibold">{formatNumber(datasources.active)}</p></div>
              <div className="rounded-lg bg-muted/50 p-3"><p className="text-muted-foreground">Исправны</p><p className="mt-1 text-xl font-semibold">{formatNumber(datasources.healthy)}</p></div>
              <div className="rounded-lg bg-muted/50 p-3"><p className="text-muted-foreground">Ошибки</p><p className="mt-1 text-xl font-semibold">{formatNumber(datasources.errors)}</p></div>
              <div className="rounded-lg bg-muted/50 p-3"><p className="text-muted-foreground">Не запускались</p><p className="mt-1 text-xl font-semibold">{formatNumber(datasources.never_synced)}</p></div>
            </div>

            {datasources.items.length === 0 ? (
              <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                Источник данных ещё не подключён. Добавьте 1С или загрузите CSV.
              </div>
            ) : (
              <div className="divide-y rounded-lg border">
                {datasources.items.slice(0, 5).map((source) => {
                  const status = sourceStatus(source.last_sync_status, source.is_active);
                  return (
                    <div key={source.id} className="flex items-start gap-3 p-3">
                      <status.icon className={cn('mt-0.5 h-5 w-5 shrink-0', status.className)} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <p className="font-medium">{source.name}</p>
                          <span className={cn('text-xs font-medium', status.className)}>{status.label}</span>
                        </div>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          Последняя синхронизация: {formatDateTime(source.last_sync_at)}
                        </p>
                        {source.last_error && <p className="mt-1 line-clamp-2 text-xs text-destructive">{source.last_error}</p>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {datasources.latest_issues.length > 0 && (
              <div className="space-y-2 rounded-lg border border-destructive/20 bg-destructive/5 p-3">
                <p className="text-sm font-medium text-destructive">Последние проблемы синхронизации</p>
                {datasources.latest_issues.slice(0, 2).map((issue) => (
                  <div key={issue.id} className="text-xs">
                    <span className="font-medium">{issue.name}:</span>{' '}
                    <span className="text-muted-foreground">{issue.message}</span>
                  </div>
                ))}
              </div>
            )}

            <Link
              href="/dashboard/settings#marketplaces"
              className="flex items-center gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50"
            >
              <Store className="h-5 w-5 text-muted-foreground" />
              <span className="min-w-0 flex-1 text-sm">Аккаунты Avito</span>
              <span className="font-semibold">{marketplaces.avito_total.toLocaleString('ru-RU')}</span>
              <ArrowRight className="h-4 w-4 text-muted-foreground" />
            </Link>
            {marketplaces.avito_truncated && (
              <p className="text-xs text-muted-foreground">
                Показаны первые {marketplaces.avito.length.toLocaleString('ru-RU')} из{' '}
                {marketplaces.avito_total.toLocaleString('ru-RU')} аккаунтов.{' '}
                <Link href="/dashboard/settings#marketplaces" className="font-medium text-primary hover:underline">
                  Открыть все
                </Link>
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ActivityIcon({ severity }: Pick<DashboardActivityItem, 'severity'>) {
  if (severity === 'success') return <CheckCircle2 className="h-5 w-5 text-green-600" />;
  if (severity === 'error') return <XCircle className="h-5 w-5 text-destructive" />;
  if (severity === 'warning') return <AlertTriangle className="h-5 w-5 text-amber-600" />;
  return <Activity className="h-5 w-5 text-blue-600" />;
}

function ActivityWidget({
  activity,
  loading,
  onRetry,
}: {
  activity: DashboardActivityItem[] | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2 text-lg">
              <Activity className="h-5 w-5" />
              Последние события
            </CardTitle>
            <CardDescription className="mt-1">Импорт, публикация и фоновые процессы.</CardDescription>
          </div>
          <Button asChild size="sm" variant="ghost"><Link href="/dashboard/logs">Все события</Link></Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? <SectionSkeleton rows={5} /> : !activity ? (
          <SectionUnavailable onRetry={onRetry} />
        ) : activity.length === 0 ? (
          <div className="rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground">
            Событий пока нет. Они появятся после первой синхронизации.
          </div>
        ) : (
          <div className="divide-y rounded-lg border">
            {activity.slice(0, 6).map((item, index) => {
              const href = safeDashboardHref(item.href);
              const repeatCount = typeof item.metadata?.repeat_count === 'number'
                && Number.isSafeInteger(item.metadata.repeat_count)
                && item.metadata.repeat_count > 1
                ? item.metadata.repeat_count
                : null;
              const windowDays = typeof item.metadata?.window_days === 'number'
                && Number.isSafeInteger(item.metadata.window_days)
                && item.metadata.window_days > 0
                ? item.metadata.window_days
                : 7;
              const content = (
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 shrink-0"><ActivityIcon severity={item.severity} /></div>
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">{item.title}</p>
                    <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground">{item.message}</p>
                    {repeatCount !== null && (
                      <p className="mt-1 text-xs font-medium text-amber-700 dark:text-amber-300">
                        Повторилось {repeatCount.toLocaleString('ru-RU')} раз за {formatDays(windowDays)}
                      </p>
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">{formatDateTime(item.occurred_at)}</p>
                  </div>
                  {href && <ArrowRight className="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />}
                </div>
              );
              return href ? (
                <Link key={`${item.code}-${item.occurred_at}-${index}`} href={href} className="block p-3 transition-colors hover:bg-muted/50">
                  {content}
                </Link>
              ) : <div key={`${item.code}-${item.occurred_at}-${index}`} className="p-3">{content}</div>;
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function UsageRow({
  label,
  used,
  limit,
}: {
  label: string;
  used: DashboardNumber | null | undefined;
  limit: DashboardNumber | null | undefined;
}) {
  const percent = dashboardPercent(used, limit);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium">
          {formatNumber(used)} {dashboardNumber(limit) === null ? '· без лимита' : `/ ${formatNumber(limit)}`}
        </span>
      </div>
      {percent !== null && <Progress value={percent} aria-label={`${label}: использовано ${percent}%`} />}
    </div>
  );
}

function ResourcesWidget({
  usage,
  loading,
  onRetry,
}: {
  usage: DashboardSummary['usage'] | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  const aiState = usage?.ai_credits
    ? dashboardAIBalanceState(usage.ai_credits)
    : 'normal';
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2 text-lg"><Coins className="h-5 w-5" /> Ресурсы тарифа</CardTitle>
            <CardDescription className="mt-1">Лимиты вынесены ниже рабочих показателей.</CardDescription>
          </div>
          <Button asChild size="sm" variant="ghost"><Link href="/dashboard/billing">Биллинг</Link></Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? <SectionSkeleton rows={3} /> : !usage ? (
          <SectionUnavailable onRetry={onRetry} />
        ) : (
          <div className="space-y-5">
            <UsageRow label="Активные объявления" used={usage.listings?.used} limit={usage.listings?.limit} />
            <UsageRow label="Товары в каталоге" used={usage.sku?.used} limit={usage.sku?.limit} />
            <div className="rounded-lg border bg-muted/30 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm text-muted-foreground">Доступно AI-кредитов</p>
                  <p className="mt-1 text-3xl font-bold">{usage.ai_credits?.unlimited ? 'Без лимита' : formatNumber(usage.ai_credits?.available_balance)}</p>
                </div>
                {aiState === 'exhausted' && <Badge variant="destructive">Баланс исчерпан</Badge>}
                {aiState === 'purchased' && <Badge variant="secondary">Используются купленные</Badge>}
                {aiState === 'included_low' && <Badge variant="secondary">Пакет заканчивается</Badge>}
              </div>
              {aiState === 'exhausted' && (
                <p className="mt-2 text-xs font-medium text-destructive">
                  Пополните баланс, чтобы продолжить AI-операции.
                </p>
              )}
              {aiState === 'purchased' && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Включённый пакет израсходован; операции списываются из купленных кредитов.
                </p>
              )}
              {aiState === 'included_low' && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Включённый пакет заканчивается. Доступный общий баланс показан выше.
                </p>
              )}
              <p className="mt-2 text-xs text-muted-foreground">
                В тарифе: {formatNumber(usage.ai_credits?.included_balance)} · Куплено: {formatNumber(usage.ai_credits?.purchased_balance)} · Зарезервировано: {formatNumber(usage.ai_credits?.reserved_balance)}
              </p>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ImageProcessingSoonWidget({ service }: { service: DashboardSummary['services']['image_processing'] | undefined }) {
  return (
    <Card className="overflow-hidden border-dashed bg-gradient-to-br from-primary/5 via-background to-violet-500/5">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="rounded-lg bg-primary/10 p-2 text-primary"><WandSparkles className="h-6 w-6" /></div>
          <Badge variant="secondary">Скоро</Badge>
        </div>
        <CardTitle role="heading" aria-level={2} className="pt-3 text-lg">{service?.title || 'AI-обработка изображений'}</CardTitle>
        <CardDescription>
          {service?.description || 'Улучшение найденных и загруженных вручную изображений перед публикацией.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-lg border bg-background/70 p-3 text-sm text-muted-foreground">
          {service?.uses_shared_ai_balance === false
            ? 'Перед запуском вы увидите доступность и стоимость операции.'
            : 'Обработка будет расходовать общий AI-баланс организации. Перед запуском вы увидите стоимость операции.'}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" disabled><Sparkles className="h-4 w-4" /> Запустить обработку</Button>
          <Button asChild variant="outline"><Link href="/dashboard/media">Подробнее</Link></Button>
        </div>
      </CardContent>
    </Card>
  );
}

function QuickActions() {
  const actions = [
    { title: 'Подключить источник', description: '1С, XML или CSV', href: '/dashboard/settings#datasources', icon: Database },
    { title: 'Загрузить CSV', description: 'Импортировать каталог', href: '/dashboard/settings#datasources', icon: Upload },
    { title: 'Настроить Avito', description: 'Аккаунт и Автозагрузка', href: '/dashboard/settings#marketplaces', icon: Store },
    { title: 'Открыть проверку', description: 'Разобрать спорные данные', href: '/dashboard/review', icon: ListChecks },
  ];
  return (
    <section aria-labelledby="quick-actions-title">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 id="quick-actions-title" className="text-lg font-semibold">Быстрые действия</h2>
          <p className="text-sm text-muted-foreground">Переходы без запуска изменений от имени пользователя.</p>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {actions.map((action) => (
          <Link
            key={action.title}
            href={action.href}
            className="group flex items-center gap-3 rounded-xl border bg-card p-4 shadow-sm transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <div className="rounded-lg bg-primary/10 p-2 text-primary"><action.icon className="h-5 w-5" /></div>
            <div className="min-w-0 flex-1">
              <p className="font-medium">{action.title}</p>
              <p className="text-xs text-muted-foreground">{action.description}</p>
            </div>
            <ArrowRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
          </Link>
        ))}
      </div>
    </section>
  );
}

export default function DashboardPage() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const hasSummary = useRef(false);
  const mounted = useRef(true);

  const load = useCallback(async (signal?: AbortSignal) => {
    const currentRequest = requestId.current + 1;
    requestId.current = currentRequest;
    if (hasSummary.current) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const response = await dashboardApi.getSummary(signal);
      if (!mounted.current || signal?.aborted || requestId.current !== currentRequest) return;
      const nextSummary = response.data?.data;
      if (!nextSummary || typeof nextSummary !== 'object') {
        throw new Error('Некорректный ответ сервера');
      }
      setSummary(nextSummary);
      hasSummary.current = true;
    } catch (loadError) {
      if (!mounted.current || signal?.aborted || requestId.current !== currentRequest) return;
      setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить дашборд');
    } finally {
      if (mounted.current && !signal?.aborted && requestId.current === currentRequest) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void load(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      mounted.current = false;
      requestId.current += 1;
      controller.abort();
    };
  }, [load]);

  const subscription = summary?.subscription;
  const planLabel = subscription?.plan ? (PLAN_LABELS[subscription.plan] ?? subscription.plan) : null;
  const status = subscription?.status ?? null;
  const attentionItems = subscription?.access_mode === 'billing_only'
    ? summary?.attention?.filter((item) => item.code !== 'subscription_inactive')
    : summary?.attention;
  const retry = () => { void load(); };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight">Дашборд</h1>
          <p className="text-muted-foreground">Продажи, публикация и задачи организации в одном месте.</p>
          <p className="mt-1 text-xs text-muted-foreground" aria-live="polite">
            {summary?.generated_at ? `Обновлено ${formatDateTime(summary.generated_at)}` : loading ? 'Обновляем данные…' : 'Время обновления неизвестно'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {planLabel && status && (
            <>
              <span className="text-sm text-muted-foreground">{planLabel}</span>
              <Badge variant={STATUS_VARIANTS[status] ?? 'secondary'}>
                {STATUS_LABELS[status] ?? status}
                {(status === 'trial' || status === 'active') && subscription?.current_period_days_left != null
                  ? ` · осталось ${formatDays(subscription.current_period_days_left)}`
                  : ''}
              </Badge>
            </>
          )}
          <Button type="button" size="sm" variant="outline" onClick={retry} disabled={loading || refreshing}>
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Обновить
          </Button>
        </div>
      </div>

      {error && (
        <div role="alert" className="flex flex-col gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <CircleAlert className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
            <div>
              <p className="font-medium">{summary ? 'Не удалось обновить данные' : 'Не удалось загрузить дашборд'}</p>
              <p className="text-muted-foreground">
                {summary
                  ? 'Предыдущие показатели сохранены. Проверьте соединение и повторите запрос.'
                  : 'Проверьте соединение и повторите запрос. Недоступные показатели не заменены нулями.'}
              </p>
            </div>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={retry}>Повторить</Button>
        </div>
      )}

      {!loading && subscription?.access_mode === 'billing_only' && (
        <div className="flex flex-col gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="font-semibold text-destructive">Подписка неактивна</p>
            <p className="text-muted-foreground">
              Просмотр данных и оплата доступны, но импорт, публикация и AI-операции заблокированы.
              {subscription.grace_days_left != null && subscription.grace_days_left > 0
                ? ` До отмены подписки осталось ${formatDays(subscription.grace_days_left)}.`
                : ''}
            </p>
          </div>
          <Button asChild size="sm"><Link href="/dashboard/billing">Восстановить доступ</Link></Button>
        </div>
      )}

      <AttentionWidget
        items={attentionItems}
        loading={loading}
        onRetry={retry}
        emptyTitle={subscription?.access_mode === 'billing_only' ? 'Других критичных задач нет' : undefined}
        emptyDescription={subscription?.access_mode === 'billing_only' ? 'После оплаты полный доступ будет восстановлен.' : undefined}
      />

      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1.65fr)_minmax(320px,0.75fr)]">
        <AnalyticsWidget analytics={summary?.analytics} loading={loading} onRetry={retry} />
        <FunnelWidget funnel={summary?.funnel} loading={loading} onRetry={retry} />
      </div>

      <div className="grid min-w-0 gap-6 xl:grid-cols-2">
        <SourcesWidget
          datasources={summary?.datasources}
          marketplaces={summary?.marketplaces}
          loading={loading}
          onRetry={retry}
        />
        <ActivityWidget activity={summary?.activity} loading={loading} onRetry={retry} />
      </div>

      <QuickActions />

      <div className="grid gap-6 xl:grid-cols-2">
        <ResourcesWidget usage={summary?.usage} loading={loading} onRetry={retry} />
        <ImageProcessingSoonWidget service={summary?.services?.image_processing} />
      </div>
    </div>
  );
}
