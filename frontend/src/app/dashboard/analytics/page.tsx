'use client';

import { useEffect, useState } from 'react';
import { analyticsApi } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { BarChart3, Eye, Phone, TrendingUp, ListOrdered } from 'lucide-react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';

interface Summary {
  views: number;
  contacts: number;
  impressions: number;
  avg_ctr: number;
  active_listings: number;
}

interface DailyPoint {
  date: string;
  views: number;
  contacts: number;
  impressions: number;
}

interface AnalyticsData {
  summary: Summary;
  daily: DailyPoint[];
  date_from: string;
  date_to: string;
}

function kpi(n: number) {
  return n.toLocaleString('ru-RU');
}

function toISODate(d: Date) {
  return d.toISOString().slice(0, 10);
}

function defaultDateRange() {
  const today = new Date();
  return {
    from: toISODate(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)),
    to: toISODate(today),
  };
}

export default function AnalyticsPage() {
  const [initialRange] = useState(defaultDateRange);
  const [dateFrom, setDateFrom] = useState(initialRange.from);
  const [dateTo, setDateTo] = useState(initialRange.to);
  const [request, setRequest] = useState({ ...initialRange, revision: 0 });
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    analyticsApi
      .get({ date_from: request.from, date_to: request.to })
      .then((response) => {
        if (active) setData(response.data.data);
      })
      .catch(() => {
        if (active) setData(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [request]);

  function applyDateRange() {
    setLoading(true);
    setRequest((current) => ({
      from: dateFrom,
      to: dateTo,
      revision: current.revision + 1,
    }));
  }

  const summary = data?.summary;
  const daily = data?.daily ?? [];

  const hasData = daily.some((d) => d.views > 0 || d.contacts > 0 || d.impressions > 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Аналитика</h1>
        <p className="text-muted-foreground">Уникальные и все просмотры, а также контакты на Avito</p>
      </div>

      {/* Фильтр дат */}
      <div className="grid gap-3 sm:flex sm:flex-wrap sm:items-end">
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">С</label>
          <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-full sm:w-40" />
        </div>
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">По</label>
          <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-full sm:w-40" />
        </div>
        <Button onClick={applyDateRange} disabled={loading} className="w-full sm:w-auto">
          Применить
        </Button>
      </div>

      {/* KPI-карточки */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
        {loading ? (
          Array.from({ length: 5 }).map((_, i) => (
            <Card key={i}>
              <CardHeader className="pb-2">
                <Skeleton className="h-4 w-24" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-8 w-16" />
              </CardContent>
            </Card>
          ))
        ) : (
          <>
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Уникальные просмотры</CardTitle>
                <Eye className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{kpi(summary?.views ?? 0)}</div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Контакты</CardTitle>
                <Phone className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{kpi(summary?.contacts ?? 0)}</div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Все просмотры</CardTitle>
                <BarChart3 className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{kpi(summary?.impressions ?? 0)}</div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Доля уникальных</CardTitle>
                <TrendingUp className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{summary?.avg_ctr ?? 0}%</div>
                <p className="mt-1 text-xs text-muted-foreground">Уникальные / все просмотры</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Активных листингов</CardTitle>
                <ListOrdered className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{kpi(summary?.active_listings ?? 0)}</div>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* График */}
      <Card>
        <CardHeader>
          <CardTitle>Уникальные, все просмотры и контакты по дням</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <Skeleton className="h-64 w-full" />
          ) : !hasData ? (
            <div className="flex h-64 flex-col items-center justify-center gap-3 text-muted-foreground">
              <BarChart3 className="h-12 w-12 opacity-20" />
              <p className="text-sm">Статистика Avito пока не собрана.</p>
              <p className="text-xs opacity-70">
                После подключения аккаунта Avito данные появятся автоматически.
              </p>
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={daily} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 12 }}
                  tickFormatter={(v) => new Date(v).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })}
                />
                <YAxis tick={{ fontSize: 12 }} allowDecimals={false} />
                <Tooltip
                  labelFormatter={(v) => new Date(v as string).toLocaleDateString('ru-RU')}
                  formatter={(value, name) => {
                    const labels: Record<string, string> = {
                      views: 'Уникальные просмотры',
                      contacts: 'Контакты',
                      impressions: 'Все просмотры',
                    };
                    const label = labels[name as string] ?? String(name);
                    return [Number(value).toLocaleString('ru-RU'), label];
                  }}
                />
                <Legend
                  formatter={(v) => (({ views: 'Уникальные просмотры', contacts: 'Контакты', impressions: 'Все просмотры' } as Record<string, string>)[v] ?? v)}
                />
                <Line type="monotone" dataKey="views" stroke="#6366f1" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="contacts" stroke="#22c55e" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="impressions" stroke="#f59e0b" strokeWidth={1.5} dot={false} strokeDasharray="4 2" />
              </LineChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
