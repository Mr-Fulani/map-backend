'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, ArrowDownUp, CheckCircle2, ChevronDown, ChevronUp,
  ExternalLink, Filter, Globe2, Loader2, PackageCheck, RefreshCw, Store,
} from 'lucide-react';
import { toast } from 'sonner';

import { listingApi, webResearchApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';

interface Difference {
  amount: string;
  percent: string;
  direction: 'above' | 'below' | 'equal';
}

interface CatalogOffer {
  source_id: string;
  source_label: string;
  status: string;
  status_label: string;
  price: string | null;
  currency: string;
  price_is_from: boolean;
  availability: string;
  availability_label: string;
  availability_text: string;
  quantity: number | null;
  checked_at: string | null;
  url: string;
  difference_from_listing: Difference | null;
  difference_from_base: Difference | null;
  message: string;
}

interface InternetOffer {
  id: number;
  provider_id: string;
  seller_name: string;
  domain: string;
  url: string;
  country_code: string;
  region: string;
  title: string;
  article: string;
  matched_code: string;
  match_type: 'exact' | 'cross' | 'analogue' | 'review';
  match_type_label: string;
  match_confidence: number;
  match_reasons: string[];
  price: string;
  currency: string;
  normalized_price: string | null;
  normalized_currency: string;
  is_price_from: boolean;
  availability: string;
  availability_label: string;
  availability_text: string;
  quantity: number | null;
  condition: string;
  condition_label: string;
  delivery_text: string;
  review_status: string;
  captured_at: string;
  expires_at: string;
  difference_from_base: Difference | null;
  difference_from_listing: Difference | null;
}

interface ResearchRun {
  id: number;
  status: string;
  offer_count: number;
  error_message: string;
}

interface Comparison {
  listing_id: number;
  product_id: number;
  base_price: string;
  listing_price: string;
  catalog_offers: CatalogOffer[];
  internet_offers: InternetOffer[];
  statistics: {
    minimum: string | null;
    median: string | null;
    maximum: string | null;
    verified_offer_count: number;
    available_seller_count: number;
    listing_vs_median: Difference | null;
    listing_vs_base: Difference | null;
    median_vs_base: Difference | null;
  };
  region: { preset: string; label: string; country_codes: string[] };
  freshness: {
    last_checked_at: string | null;
    ttl_hours: number;
    fresh_offer_count: number;
    stale_offer_count: number;
  };
  active_run: ResearchRun | null;
  latest_run: ResearchRun | null;
  warnings: string[];
}

const RUN_STATUS: Record<string, string> = {
  queued: 'В очереди', running: 'Выполняется', need_review: 'Требуется проверка',
  completed: 'Предложения обновлены', no_results: 'Ничего не найдено',
  failed: 'Ошибка провайдера', skipped: 'Поиск не потребовался',
};

const COUNTRY_LABELS: Record<string, string> = {
  RU: 'Россия', BY: 'Беларусь', KZ: 'Казахстан', AM: 'Армения', KG: 'Кыргызстан',
  UZ: 'Узбекистан', AZ: 'Азербайджан', MD: 'Молдова', TJ: 'Таджикистан',
};

function rubles(value: string | null | undefined) {
  if (!value) return '—';
  return `${Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽`;
}

function dateTime(value: string | null | undefined) {
  if (!value) return 'Ещё не обновлялось';
  return new Date(value).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}

function DifferenceBadge({ difference }: { difference: Difference | null }) {
  if (!difference) return null;
  const positive = difference.direction === 'below';
  const className = positive
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
    : difference.direction === 'above'
      ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
      : '';
  return (
    <Badge variant="outline" className={className}>
      {difference.direction === 'above' ? '+' : difference.direction === 'below' ? '−' : ''}
      {rubles(String(Math.abs(Number(difference.amount))))} · {Math.abs(Number(difference.percent))}%
    </Badge>
  );
}

function percentHint(difference: Difference | null, suffix: string) {
  if (!difference) return undefined;
  const sign = difference.direction === 'above' ? '+' : difference.direction === 'below' ? '−' : '';
  return `${sign}${Math.abs(Number(difference.percent))}% ${suffix}`;
}

function SummaryCard({ label, value, hint, tone = 'neutral' }: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'neutral' | 'positive' | 'warning';
}) {
  return (
    <div className={`rounded-xl border p-3 ${
      tone === 'positive' ? 'border-emerald-500/25 bg-emerald-500/5'
        : tone === 'warning' ? 'border-amber-500/25 bg-amber-500/5' : 'bg-card'
    }`}>
      <p className="text-[11px] leading-tight text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-1 text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Filters({
  countries, country, setCountry, onlyInStock, setOnlyInStock,
  onlyNew, setOnlyNew, showAnalogues, setShowAnalogues,
}: {
  countries: string[];
  country: string;
  setCountry: (value: string) => void;
  onlyInStock: boolean;
  setOnlyInStock: (value: boolean) => void;
  onlyNew: boolean;
  setOnlyNew: (value: boolean) => void;
  showAnalogues: boolean;
  setShowAnalogues: (value: boolean) => void;
}) {
  return (
    <div className="grid gap-3 md:grid-cols-4 md:items-center">
      <label className="grid gap-1 text-xs text-muted-foreground">
        Страна
        <select
          value={country}
          onChange={(event) => setCountry(event.target.value)}
          className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"
        >
          <option value="">Все страны</option>
          {countries.map((code) => <option key={code} value={code}>{COUNTRY_LABELS[code] ?? code}</option>)}
        </select>
      </label>
      <label className="flex items-center justify-between gap-3 text-sm">
        Только в наличии
        <Switch checked={onlyInStock} onCheckedChange={setOnlyInStock} />
      </label>
      <label className="flex items-center justify-between gap-3 text-sm">
        Только новые
        <Switch checked={onlyNew} onCheckedChange={setOnlyNew} />
      </label>
      <label className="flex items-center justify-between gap-3 text-sm">
        Возможные аналоги
        <Switch checked={showAnalogues} onCheckedChange={setShowAnalogues} />
      </label>
    </div>
  );
}

export default function MarketPricingPanel({
  listingId, listingStatus, onApplyPrice,
}: {
  listingId: number;
  listingStatus: string;
  onApplyPrice: (price: string) => void;
}) {
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshingRunId, setRefreshingRunId] = useState<number | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [sort, setSort] = useState<'price' | 'availability' | 'confidence'>('price');
  const [country, setCountry] = useState('');
  const [onlyInStock, setOnlyInStock] = useState(false);
  const [onlyNew, setOnlyNew] = useState(false);
  const [showAnalogues, setShowAnalogues] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await listingApi.marketComparison(listingId);
      const data = response.data.data as Comparison;
      setComparison(data);
      if (data.active_run) setRefreshingRunId(data.active_run.id);
    } catch {
      setComparison(null);
      toast.error('Не удалось загрузить сравнение рынка');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [listingId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!refreshingRunId) return;
    const interval = window.setInterval(async () => {
      try {
        const response = await productPoll(refreshingRunId);
        const run = response.data.data as ResearchRun;
        if (!['queued', 'running'].includes(run.status)) {
          window.clearInterval(interval);
          setRefreshingRunId(null);
          await load(true);
          if (run.status === 'failed') toast.error(run.error_message || 'Ошибка поискового провайдера');
          else toast.success(run.status === 'no_results' ? 'Новых предложений не найдено' : 'Предложения обновлены');
        }
      } catch {
        window.clearInterval(interval);
        setRefreshingRunId(null);
        toast.error('Не удалось получить статус исследования');
      }
    }, 2500);
    return () => window.clearInterval(interval);
  }, [load, refreshingRunId]);

  const productPoll = (runId: number) => webResearchApi.get(runId);

  const refresh = async () => {
    if (!comparison) return;
    try {
      const response = await webResearchApi.startMarketResearch(comparison.product_id, true);
      const run = response.data.data as ResearchRun | null;
      if (run && ['queued', 'running'].includes(run.status)) {
        setRefreshingRunId(run.id);
        toast.success('Поиск предложений запущен');
      } else {
        await load(true);
      }
    } catch (error: unknown) {
      const message = (error as { response?: { data?: { message?: string } } })?.response?.data?.message;
      toast.error(message || 'Не удалось запустить исследование');
    }
  };

  const countries = useMemo(() => (
    Array.from(new Set((comparison?.internet_offers ?? []).map((offer) => offer.country_code).filter(Boolean))).sort()
  ), [comparison]);

  const filtered = useMemo(() => {
    const result = (comparison?.internet_offers ?? []).filter((offer) => (
      (!country || offer.country_code === country)
      && (!onlyInStock || offer.availability === 'in_stock')
      && (!onlyNew || offer.condition === 'new')
      && (showAnalogues || offer.match_type !== 'analogue')
    ));
    return result.sort((left, right) => {
      if (sort === 'confidence') return right.match_confidence - left.match_confidence;
      if (sort === 'availability') {
        const rank: Record<string, number> = { in_stock: 0, preorder: 1, unknown: 2, out_of_stock: 3 };
        return (rank[left.availability] ?? 4) - (rank[right.availability] ?? 4);
      }
      return Number(left.normalized_price ?? Number.MAX_SAFE_INTEGER)
        - Number(right.normalized_price ?? Number.MAX_SAFE_INTEGER);
    });
  }, [comparison, country, onlyInStock, onlyNew, showAnalogues, sort]);

  if (loading) {
    return <div className="space-y-4 p-4 sm:p-6"><Skeleton className="h-24" /><Skeleton className="h-48" /><Skeleton className="h-72" /></div>;
  }

  if (!comparison) {
    return (
      <div className="flex min-h-[360px] items-center justify-center p-8 text-center">
        <div><AlertTriangle className="mx-auto h-8 w-8 text-amber-500" /><p className="mt-3 font-medium">Сравнение временно недоступно</p><Button className="mt-4" variant="outline" onClick={() => load()}>Повторить</Button></div>
      </div>
    );
  }

  const difference = comparison.statistics.listing_vs_median;
  const differenceTone = difference?.direction === 'below' ? 'positive' : difference?.direction === 'above' ? 'warning' : 'neutral';
  const visibleOffers = expanded ? filtered : filtered.slice(0, 6);

  return (
    <div className="space-y-6 p-4 pb-28 sm:p-6 sm:pb-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Рынок и цены</h2>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1"><Globe2 className="h-3.5 w-3.5" />{comparison.region.label}</span>
            <span>Обновлено: {dateTime(comparison.freshness.last_checked_at)}</span>
          </div>
        </div>
        <Button onClick={refresh} disabled={refreshingRunId !== null} className="shrink-0">
          {refreshingRunId ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
          {refreshingRunId ? 'Исследуем рынок' : 'Обновить предложения'}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/25 p-3 text-sm">
        {refreshingRunId ? <Loader2 className="h-4 w-4 animate-spin text-primary" /> : comparison.latest_run?.status === 'completed' ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <Store className="h-4 w-4 text-muted-foreground" />}
        <span className="font-medium">{RUN_STATUS[refreshingRunId ? 'running' : comparison.latest_run?.status ?? 'no_results']}</span>
        <span className="text-muted-foreground">· {comparison.freshness.fresh_offer_count} свежих предложений</span>
      </div>

      {comparison.warnings.length > 0 && (
        <div className="space-y-1 rounded-lg border border-amber-500/25 bg-amber-500/5 p-3 text-xs text-amber-800 dark:text-amber-200">
          {comparison.warnings.map((warning) => <p key={warning}>{warning}</p>)}
        </div>
      )}

      <section className="space-y-3">
        <h3 className="text-sm font-medium">Сводка цен</h3>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3 2xl:grid-cols-4">
          <SummaryCard label="Базовая цена товара" value={rubles(comparison.base_price)} />
          <SummaryCard
            label="Цена объявления"
            value={rubles(comparison.listing_price)}
            hint={percentHint(comparison.statistics.listing_vs_base, 'к базовой цене')}
            tone={comparison.statistics.listing_vs_base?.direction === 'above' ? 'warning' : 'positive'}
          />
          <SummaryCard label="Минимум подтверждённый" value={rubles(comparison.statistics.minimum)} />
          <SummaryCard
            label="Медиана рынка"
            value={rubles(comparison.statistics.median)}
            hint={percentHint(comparison.statistics.median_vs_base, 'к базовой цене')}
          />
          <SummaryCard label="Максимум подтверждённый" value={rubles(comparison.statistics.maximum)} />
          <SummaryCard label="Продавцов в наличии" value={String(comparison.statistics.available_seller_count)} hint={`${comparison.statistics.verified_offer_count} цен в статистике`} />
          <SummaryCard
            label="Объявление к медиане"
            value={difference ? `${difference.direction === 'above' ? '+' : difference.direction === 'below' ? '−' : ''}${rubles(String(Math.abs(Number(difference.amount))))}` : '—'}
            hint={difference ? `${Math.abs(Number(difference.percent))}%` : 'Недостаточно данных'}
            tone={differenceTone}
          />
        </div>
      </section>

      <section className="space-y-3">
        <div><h3 className="text-sm font-medium">Каталоги запчастей</h3><p className="text-xs text-muted-foreground">Последние проверки Tachka, Rossko и Euroauto</p></div>
        <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3">
          {comparison.catalog_offers.map((offer) => (
            <article key={offer.source_id} className="rounded-xl border bg-card p-4">
              <div className="flex items-start justify-between gap-2"><p className="font-medium">{offer.source_label}</p><Badge variant="outline">{offer.availability_label}</Badge></div>
              <p className="mt-3 text-xl font-semibold tabular-nums">{offer.price_is_from && <span className="mr-1 text-xs font-normal text-muted-foreground">от</span>}{offer.price ? `${Number(offer.price).toLocaleString('ru-RU')} ${offer.currency === 'RUB' ? '₽' : offer.currency}` : '—'}</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {offer.difference_from_base && <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">К базе <DifferenceBadge difference={offer.difference_from_base} /></span>}
                {offer.difference_from_listing && <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">Объявление <DifferenceBadge difference={offer.difference_from_listing} /></span>}
              </div>
              <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                {offer.quantity !== null && <p>Количество: {offer.quantity}</p>}
                <p>Проверено: {dateTime(offer.checked_at)}</p>
                {offer.message && <p className="text-amber-700 dark:text-amber-300">{offer.message}</p>}
              </div>
              {offer.url && <a href={offer.url} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1 text-xs text-primary hover:underline">Источник <ExternalLink className="h-3 w-3" /></a>}
            </article>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div><h3 className="text-sm font-medium">Предложения из интернета</h3><p className="text-xs text-muted-foreground">Сомнительные совпадения помечены и не участвуют в статистике</p></div>
          <div className="flex gap-2">
            <label className="grid gap-1 text-[11px] text-muted-foreground"><span className="inline-flex items-center gap-1"><ArrowDownUp className="h-3 w-3" />Сортировка</span><select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)} className="h-9 rounded-md border bg-background px-3 text-sm text-foreground"><option value="price">По цене</option><option value="availability">По наличию</option><option value="confidence">По точности</option></select></label>
            <Button variant="outline" size="sm" className="mt-auto md:hidden" onClick={() => setFilterOpen(true)}><Filter className="mr-2 h-4 w-4" />Фильтры</Button>
          </div>
        </div>
        <div className="hidden rounded-lg border bg-muted/20 p-3 md:block"><Filters countries={countries} country={country} setCountry={setCountry} onlyInStock={onlyInStock} setOnlyInStock={setOnlyInStock} onlyNew={onlyNew} setOnlyNew={setOnlyNew} showAnalogues={showAnalogues} setShowAnalogues={setShowAnalogues} /></div>

        {visibleOffers.length === 0 ? (
          <div className="rounded-xl border border-dashed p-8 text-center"><PackageCheck className="mx-auto h-8 w-8 text-muted-foreground" /><p className="mt-3 text-sm font-medium">Подходящих предложений нет</p><p className="mt-1 text-xs text-muted-foreground">Измените фильтры или обновите исследование.</p></div>
        ) : (
          <div className="grid gap-3 2xl:grid-cols-2">
            {visibleOffers.map((offer) => (
              <article key={offer.id} className="rounded-xl border bg-card p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0"><p className="truncate font-medium">{offer.seller_name || offer.domain}</p><p className="truncate text-xs text-muted-foreground">{offer.domain} {offer.country_code && `· ${COUNTRY_LABELS[offer.country_code] ?? offer.country_code}`}</p></div>
                  <Badge variant={offer.review_status === 'verified' ? 'secondary' : 'outline'} className={offer.review_status === 'verified' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : 'border-amber-500/30 text-amber-700 dark:text-amber-300'}>{offer.match_type_label}</Badge>
                </div>
                <p className="mt-3 line-clamp-2 text-sm">{offer.title || 'Название не указано'}</p>
                <div className="mt-3 flex flex-wrap items-end justify-between gap-2"><div><p className="text-xl font-semibold tabular-nums">{offer.is_price_from && <span className="mr-1 text-xs font-normal text-muted-foreground">от</span>}{Number(offer.price).toLocaleString('ru-RU')} {offer.currency === 'RUB' ? '₽' : offer.currency}</p>{offer.currency !== 'RUB' && <p className="text-xs text-muted-foreground">{offer.normalized_price ? `≈ ${rubles(offer.normalized_price)}` : 'Конвертация недоступна'}</p>}</div><Badge variant="outline">{offer.availability_label}</Badge></div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {offer.difference_from_base && <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">К базе <DifferenceBadge difference={offer.difference_from_base} /></span>}
                  {offer.difference_from_listing && <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">Объявление <DifferenceBadge difference={offer.difference_from_listing} /></span>}
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs"><dt className="text-muted-foreground">Состояние</dt><dd>{offer.condition_label}</dd><dt className="text-muted-foreground">Точность</dt><dd>{Math.round(offer.match_confidence * 100)}%</dd><dt className="text-muted-foreground">Найдено по</dt><dd className="truncate" title={offer.matched_code || offer.article}>{offer.matched_code || offer.article || 'не подтверждено'}</dd><dt className="text-muted-foreground">Проверено</dt><dd>{dateTime(offer.captured_at)}</dd>{offer.quantity !== null && <><dt className="text-muted-foreground">Количество</dt><dd>{offer.quantity}</dd></>}{offer.delivery_text && <><dt className="text-muted-foreground">Доставка</dt><dd>{offer.delivery_text}</dd></>}</dl>
                {offer.match_reasons.length > 0 && <p className="mt-3 line-clamp-2 text-xs text-muted-foreground">{offer.match_reasons.join(' · ')}</p>}
                <div className="mt-4 flex flex-wrap gap-2"><Button size="sm" variant="outline" asChild><a href={offer.url} target="_blank" rel="noreferrer">Открыть <ExternalLink className="ml-1 h-3.5 w-3.5" /></a></Button><Button size="sm" onClick={() => { onApplyPrice(offer.normalized_price!); toast.success(listingStatus === 'active' ? 'Цена подготовлена. После сохранения отправим безопасное обновление в Avito.' : 'Цена подставлена в черновик объявления.'); }} disabled={!offer.normalized_price}>Подставить цену</Button></div>
                <p className="mt-2 text-[11px] text-muted-foreground">Источник поиска: {offer.provider_id || 'не указан'}. Подстановка не публикует изменение автоматически.</p>
              </article>
            ))}
          </div>
        )}
        {filtered.length > 6 && <Button variant="ghost" className="w-full" onClick={() => setExpanded((value) => !value)}>{expanded ? <ChevronUp className="mr-2 h-4 w-4" /> : <ChevronDown className="mr-2 h-4 w-4" />}{expanded ? 'Свернуть предложения' : `Показать ещё ${filtered.length - 6}`}</Button>}
      </section>

      <div className="fixed inset-x-0 bottom-0 z-20 border-t bg-background/95 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur md:hidden">
        <Button onClick={refresh} disabled={refreshingRunId !== null} className="w-full">{refreshingRunId ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}{refreshingRunId ? 'Исследуем рынок' : 'Обновить предложения'}</Button>
      </div>

      <Sheet open={filterOpen} onOpenChange={setFilterOpen}>
        <SheetContent side="bottom" className="rounded-t-2xl pb-[max(1.5rem,env(safe-area-inset-bottom))]">
          <SheetHeader><SheetTitle>Фильтры предложений</SheetTitle></SheetHeader>
          <div className="mt-5"><Filters countries={countries} country={country} setCountry={setCountry} onlyInStock={onlyInStock} setOnlyInStock={setOnlyInStock} onlyNew={onlyNew} setOnlyNew={setOnlyNew} showAnalogues={showAnalogues} setShowAnalogues={setShowAnalogues} /></div>
          <Button className="mt-6 w-full" onClick={() => setFilterOpen(false)}>Показать {filtered.length}</Button>
        </SheetContent>
      </Sheet>
    </div>
  );
}
