'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, ArrowDownUp, CheckCircle2, ChevronDown, ChevronUp,
  ExternalLink, Filter, Globe2, Loader2, PackageCheck, RefreshCw, Store,
} from 'lucide-react';
import { toast } from 'sonner';

import { productApi, webResearchApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
  RESEARCH_COUNTRY_LABELS,
  ResearchGeographyPicker,
  type ResearchRegionPreset,
} from '@/components/settings/research-geography-picker';

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
  difference_from_reference: Difference | null;
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
  difference_from_reference: Difference | null;
}

interface ResearchRun {
  id: number;
  status: string;
  offer_count: number;
  error_message: string;
}

interface TenantResearchSettings {
  region_preset: ResearchRegionPreset;
  region_label: string;
  country_codes: string[];
}

interface Comparison {
  listing_id: number | null;
  product_id: number;
  base_price: string;
  reference_price: string;
  catalog_offers_applicable: boolean;
  catalog_offers: CatalogOffer[];
  internet_offers: InternetOffer[];
  statistics: {
    minimum: string | null;
    median: string | null;
    maximum: string | null;
    verified_offer_count: number;
    available_seller_count: number;
    reference_vs_median: Difference | null;
    reference_vs_base: Difference | null;
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

const FILTER_STORAGE_KEY = 'market-pricing-filters-v1';

interface SavedFilters {
  sort?: 'price' | 'availability' | 'confidence';
  country?: string;
  onlyInStock?: boolean;
  onlyNew?: boolean;
  showAnalogues?: boolean;
}

function savedFilters(): SavedFilters {
  if (typeof window === 'undefined') return {};
  try {
    return JSON.parse(window.localStorage.getItem(FILTER_STORAGE_KEY) || '{}') as SavedFilters;
  } catch {
    return {};
  }
}

function rubles(value: string | null | undefined) {
  if (!value) return '—';
  return `${Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽`;
}

function dateTime(value: string | null | undefined) {
  if (!value) return 'Ещё не обновлялось';
  return new Date(value).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}

function comparisonSentence(difference: Difference | null, reference: string) {
  if (!difference) return undefined;
  if (difference.direction === 'equal') return 'Разницы нет';
  const direction = difference.direction === 'above' ? 'Дороже' : 'Дешевле';
  const percent = Math.abs(Number(difference.percent)).toLocaleString('ru-RU');
  return `${direction} ${reference}: на ${percent}% (${rubles(String(Math.abs(Number(difference.amount))))})`;
}

function ComparisonLine({ difference, reference }: { difference: Difference | null; reference: string }) {
  const text = comparisonSentence(difference, reference);
  if (!text) return null;
  const className = difference?.direction === 'below'
    ? 'border-emerald-500/25 bg-emerald-500/5 text-emerald-800 dark:text-emerald-200'
    : difference?.direction === 'above'
      ? 'border-amber-500/25 bg-amber-500/5 text-amber-800 dark:text-amber-200'
      : 'bg-muted/30 text-muted-foreground';
  return <p className={`rounded-md border px-2.5 py-1.5 text-xs leading-relaxed ${className}`}>{text}</p>;
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
  onlyNew, setOnlyNew, showAnalogues, setShowAnalogues, totalOffers,
}: {
  countries: Array<{ code: string; count: number }>;
  country: string;
  setCountry: (value: string) => void;
  onlyInStock: boolean;
  setOnlyInStock: (value: boolean) => void;
  onlyNew: boolean;
  setOnlyNew: (value: boolean) => void;
  showAnalogues: boolean;
  setShowAnalogues: (value: boolean) => void;
  totalOffers: number;
}) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 rounded-lg border bg-card p-3 sm:grid-cols-[minmax(0,280px)_1fr] sm:items-center">
        <div>
          <p className="text-sm font-medium">Страна найденного продавца</p>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
            Фильтрует уже найденные предложения и не меняет географию следующего поиска.
          </p>
        </div>
        <Select value={country || 'all'} onValueChange={(value) => setCountry(value === 'all' ? '' : value)}>
          <SelectTrigger aria-label="Страна найденного продавца" className="h-10 bg-background">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="z-[80]">
            <SelectItem value="all">Все найденные страны · {totalOffers}</SelectItem>
            {countries.map(({ code, count }) => (
              <SelectItem key={code} value={code}>
                {RESEARCH_COUNTRY_LABELS[code] ?? code} · {count}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {[
          { title: 'Только в наличии', hint: 'Скрыть отсутствующие', checked: onlyInStock, change: setOnlyInStock },
          { title: 'Только новые', hint: 'Скрыть товары б/у', checked: onlyNew, change: setOnlyNew },
          { title: 'Возможные аналоги', hint: 'Показать неточные замены', checked: showAnalogues, change: setShowAnalogues },
        ].map((item) => (
          <div key={item.title} className={`flex min-h-20 items-center justify-between gap-3 rounded-lg border p-3 transition-colors ${item.checked ? 'border-primary/35 bg-primary/5' : 'bg-card'}`}>
            <span>
              <span className="block text-sm font-medium">{item.title}</span>
              <span className="mt-1 block text-[11px] text-muted-foreground">{item.checked ? 'Включено' : item.hint}</span>
            </span>
            <Switch aria-label={item.title} checked={item.checked} onCheckedChange={item.change} />
          </div>
        ))}
      </div>
    </div>
  );
}

export default function MarketPricingPanel({
  productId,
  referencePrice,
  channelLabel,
  channelStatus,
  onApplyPrice,
  refreshKey = 0,
}: {
  productId: number;
  referencePrice: string;
  channelLabel: string;
  channelStatus?: string;
  onApplyPrice: (price: string) => void;
  refreshKey?: number;
}) {
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshingRunId, setRefreshingRunId] = useState<number | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [initialFilters] = useState(savedFilters);
  const [sort, setSort] = useState<'price' | 'availability' | 'confidence'>(initialFilters.sort ?? 'price');
  const [country, setCountry] = useState(initialFilters.country ?? '');
  const [onlyInStock, setOnlyInStock] = useState(initialFilters.onlyInStock ?? false);
  const [onlyNew, setOnlyNew] = useState(initialFilters.onlyNew ?? false);
  const [showAnalogues, setShowAnalogues] = useState(initialFilters.showAnalogues ?? false);
  const [expanded, setExpanded] = useState(false);
  const [researchSettings, setResearchSettings] = useState<TenantResearchSettings | null>(null);
  const [canEditGeography, setCanEditGeography] = useState(false);
  const [savingGeography, setSavingGeography] = useState(false);

  const requestComparison = useCallback(async () => {
    void refreshKey;
    const response = await productApi.marketComparison(productId, referencePrice);
    return response.data.data as Comparison;
  }, [productId, referencePrice, refreshKey]);

  const applyComparison = useCallback((data: Comparison) => {
    setComparison(data);
    if (data.active_run) setRefreshingRunId(data.active_run.id);
    const availableCountries = new Set(
      data.internet_offers.map((offer) => offer.country_code).filter(Boolean),
    );
    setCountry((current) => current && !availableCountries.has(current) ? '' : current);
  }, []);

  const load = useCallback(async () => {
    try {
      applyComparison(await requestComparison());
    } catch {
      setComparison(null);
      toast.error('Не удалось загрузить сравнение рынка');
    } finally {
      setLoading(false);
    }
  }, [applyComparison, requestComparison]);

  useEffect(() => {
    let cancelled = false;
    requestComparison()
      .then((data) => {
        if (!cancelled) applyComparison(data);
      })
      .catch(() => {
        if (!cancelled) {
          setComparison(null);
          toast.error('Не удалось загрузить сравнение рынка');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [applyComparison, requestComparison]);

  useEffect(() => {
    webResearchApi.settings()
      .then((response) => {
        setResearchSettings(response.data.data as TenantResearchSettings);
        setCanEditGeography(Boolean(response.data.can_edit));
      })
      .catch(() => {
        setResearchSettings(null);
      });
  }, []);

  useEffect(() => {
    window.localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({
      sort, country, onlyInStock, onlyNew, showAnalogues,
    }));
  }, [country, onlyInStock, onlyNew, showAnalogues, sort]);

  const productPoll = (runId: number) => webResearchApi.get(runId);

  useEffect(() => {
    if (!refreshingRunId) return;
    const interval = window.setInterval(async () => {
      try {
        const response = await productPoll(refreshingRunId);
        const run = response.data.data as ResearchRun;
        if (!['queued', 'running'].includes(run.status)) {
          window.clearInterval(interval);
          setRefreshingRunId(null);
          await load();
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

  const refresh = async () => {
    if (!comparison) return;
    try {
      const response = await webResearchApi.startMarketResearch(comparison.product_id, true);
      const run = response.data.data as ResearchRun | null;
      if (run && ['queued', 'running'].includes(run.status)) {
        setRefreshingRunId(run.id);
        toast.success('Поиск предложений запущен');
      } else {
        await load();
      }
    } catch (error: unknown) {
      const message = (error as { response?: { data?: { message?: string } } })?.response?.data?.message;
      toast.error(message || 'Не удалось запустить исследование');
    }
  };

  const saveGeography = async (
    regionPreset: ResearchRegionPreset,
    countryCodes: string[],
  ) => {
    if (!researchSettings || !canEditGeography) return;
    const previous = researchSettings;
    setResearchSettings({
      ...researchSettings,
      region_preset: regionPreset,
      country_codes: countryCodes,
    });
    setSavingGeography(true);
    try {
      const response = await webResearchApi.updateSettings({
        region_preset: regionPreset,
        country_codes: countryCodes,
      });
      const saved = response.data.data as TenantResearchSettings;
      setResearchSettings(saved);
      setComparison((current) => current ? {
        ...current,
        region: {
          preset: saved.region_preset,
          label: saved.region_label,
          country_codes: saved.country_codes,
        },
      } : current);
      toast.success('География сохранена. Она применится при следующем обновлении предложений.');
    } catch (error: unknown) {
      setResearchSettings(previous);
      const detail = (error as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      toast.error(detail || 'Не удалось сохранить географию поиска');
    } finally {
      setSavingGeography(false);
    }
  };

  const countries = useMemo(() => {
    const counts = new Map<string, number>();
    for (const offer of comparison?.internet_offers ?? []) {
      if (offer.country_code) counts.set(offer.country_code, (counts.get(offer.country_code) ?? 0) + 1);
    }
    return Array.from(counts, ([code, count]) => ({ code, count })).sort((left, right) => (
      (RESEARCH_COUNTRY_LABELS[left.code] ?? left.code)
        .localeCompare(RESEARCH_COUNTRY_LABELS[right.code] ?? right.code, 'ru')
    ));
  }, [comparison]);

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
        <div><AlertTriangle className="mx-auto h-8 w-8 text-amber-500" /><p className="mt-3 font-medium">Сравнение временно недоступно</p><Button className="mt-4" variant="outline" onClick={() => { setLoading(true); void load(); }}>Повторить</Button></div>
      </div>
    );
  }

  const difference = comparison.statistics.reference_vs_median;
  const differenceTone = difference?.direction === 'below' ? 'positive' : difference?.direction === 'above' ? 'warning' : 'neutral';
  const visibleOffers = expanded ? filtered : filtered.slice(0, 6);

  return (
    <div className="space-y-6 p-4 pb-28 sm:p-6 sm:pb-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Рынок и цены</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            Исследование общее для товара. Ниже оно сравнивается с ценой канала {channelLabel}.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1"><Globe2 className="h-3.5 w-3.5" />{comparison.region.label}</span>
            <span>Обновлено: {dateTime(comparison.freshness.last_checked_at)}</span>
          </div>
        </div>
        <Button onClick={refresh} disabled={refreshingRunId !== null || savingGeography} className="shrink-0">
          {refreshingRunId ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
          {refreshingRunId ? 'Исследуем рынок' : 'Обновить предложения'}
        </Button>
      </div>

      {researchSettings && (
        <ResearchGeographyPicker
          regionPreset={researchSettings.region_preset}
          countryCodes={researchSettings.country_codes}
          canEdit={canEditGeography}
          saving={savingGeography}
          compact
          onChange={saveGeography}
        />
      )}

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
          <SummaryCard label="Наша базовая цена товара" value={rubles(comparison.base_price)} />
          <SummaryCard
            label={`Цена для ${channelLabel}`}
            value={rubles(comparison.reference_price)}
            hint={comparisonSentence(comparison.statistics.reference_vs_base, 'нашей базовой цены')}
            tone={comparison.statistics.reference_vs_base?.direction === 'above' ? 'warning' : 'positive'}
          />
          <SummaryCard label="Самая низкая подтверждённая цена" value={rubles(comparison.statistics.minimum)} />
          <SummaryCard
            label="Типичная цена на рынке"
            value={rubles(comparison.statistics.median)}
            hint={comparisonSentence(comparison.statistics.median_vs_base, 'нашей базовой цены')}
          />
          <SummaryCard label="Самая высокая подтверждённая цена" value={rubles(comparison.statistics.maximum)} />
          <SummaryCard label="Продавцов с товаром в наличии" value={String(comparison.statistics.available_seller_count)} hint={`Подтверждённых предложений в расчёте: ${comparison.statistics.verified_offer_count}`} />
          <SummaryCard
            label={`Цена ${channelLabel} относительно рынка`}
            value={difference ? `${Math.abs(Number(difference.percent)).toLocaleString('ru-RU')}%` : '—'}
            hint={comparisonSentence(difference, 'типичной цены на рынке') ?? 'Недостаточно данных'}
            tone={differenceTone}
          />
        </div>
        <p className="text-xs text-muted-foreground">
          Типичная цена — середина подтверждённых предложений. Единичные слишком дешёвые или дорогие цены меньше влияют на неё.
        </p>
      </section>

      {comparison.catalog_offers_applicable && (
        <section className="space-y-3">
          <div><h3 className="text-sm font-medium">Каталоги запчастей</h3><p className="text-xs text-muted-foreground">Последние проверки Tachka, Rossko и Euroauto</p></div>
          <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3">
            {comparison.catalog_offers.map((offer) => (
              <article key={offer.source_id} className="rounded-xl border bg-card p-4">
                <div className="flex items-start justify-between gap-2"><p className="font-medium">{offer.source_label}</p><Badge variant="outline">{offer.availability_label}</Badge></div>
                <p className="mt-3 text-xl font-semibold tabular-nums">{offer.price_is_from && <span className="mr-1 text-xs font-normal text-muted-foreground">от</span>}{offer.price ? `${Number(offer.price).toLocaleString('ru-RU')} ${offer.currency === 'RUB' ? '₽' : offer.currency}` : '—'}</p>
                <div className="mt-2 grid gap-1.5">
                  <ComparisonLine difference={offer.difference_from_base} reference="нашей базовой цены" />
                  <ComparisonLine difference={offer.difference_from_reference} reference={`цены ${channelLabel}`} />
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
      )}

      <section className="space-y-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div><h3 className="text-sm font-medium">Предложения из интернета</h3><p className="text-xs text-muted-foreground">Сомнительные совпадения помечены и не участвуют в статистике</p></div>
          <div className="grid w-full grid-cols-[minmax(0,1fr)_auto] gap-2 sm:w-auto">
            <div className="grid min-w-0 gap-1 text-[11px] text-muted-foreground sm:min-w-44">
              <span className="inline-flex items-center gap-1"><ArrowDownUp className="h-3 w-3" />Сортировка</span>
              <Select value={sort} onValueChange={(value) => setSort(value as typeof sort)}>
                <SelectTrigger aria-label="Сортировка предложений" className="h-9 bg-background">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="z-[80]">
                  <SelectItem value="price">По цене</SelectItem>
                  <SelectItem value="availability">По наличию</SelectItem>
                  <SelectItem value="confidence">По точности</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button variant="outline" size="sm" className="mt-auto md:hidden" onClick={() => setFilterOpen(true)}><Filter className="mr-2 h-4 w-4" />Фильтры</Button>
          </div>
        </div>
        <div className="hidden rounded-lg border bg-muted/20 p-3 md:block"><Filters countries={countries} country={country} setCountry={setCountry} onlyInStock={onlyInStock} setOnlyInStock={setOnlyInStock} onlyNew={onlyNew} setOnlyNew={setOnlyNew} showAnalogues={showAnalogues} setShowAnalogues={setShowAnalogues} totalOffers={comparison.internet_offers.length} /></div>

        {visibleOffers.length === 0 ? (
          <div className="rounded-xl border border-dashed p-8 text-center"><PackageCheck className="mx-auto h-8 w-8 text-muted-foreground" /><p className="mt-3 text-sm font-medium">Подходящих предложений нет</p><p className="mt-1 text-xs text-muted-foreground">Измените фильтры или обновите исследование.</p></div>
        ) : (
          <div className="grid gap-3 2xl:grid-cols-2">
            {visibleOffers.map((offer) => (
              <article key={offer.id} className="rounded-xl border bg-card p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0"><p className="truncate font-medium">{offer.seller_name || offer.domain}</p><p className="truncate text-xs text-muted-foreground">{offer.domain} {offer.country_code && `· ${RESEARCH_COUNTRY_LABELS[offer.country_code] ?? offer.country_code}`}</p></div>
                  <Badge variant={offer.review_status === 'verified' ? 'secondary' : 'outline'} className={`max-w-[55%] shrink-0 whitespace-normal text-right leading-tight ${offer.review_status === 'verified' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : 'border-amber-500/30 text-amber-700 dark:text-amber-300'}`}>{offer.match_type_label}</Badge>
                </div>
                <p className="mt-3 line-clamp-2 text-sm">{offer.title || 'Название не указано'}</p>
                <div className="mt-3 flex flex-wrap items-end justify-between gap-2"><div><p className="text-xl font-semibold tabular-nums">{offer.is_price_from && <span className="mr-1 text-xs font-normal text-muted-foreground">от</span>}{Number(offer.price).toLocaleString('ru-RU')} {offer.currency === 'RUB' ? '₽' : offer.currency}</p>{offer.currency !== 'RUB' && <p className="text-xs text-muted-foreground">{offer.normalized_price ? `≈ ${rubles(offer.normalized_price)}` : 'Конвертация недоступна'}</p>}</div><Badge variant="outline">{offer.availability_label}</Badge></div>
                <div className="mt-2 grid gap-1.5">
                  <ComparisonLine difference={offer.difference_from_base} reference="нашей базовой цены" />
                  <ComparisonLine difference={offer.difference_from_reference} reference={`цены ${channelLabel}`} />
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs [&_dd]:min-w-0 [&_dd]:break-words"><dt className="text-muted-foreground">Состояние</dt><dd>{offer.condition_label}</dd><dt className="text-muted-foreground">Точность</dt><dd>{Math.round(offer.match_confidence * 100)}%</dd><dt className="text-muted-foreground">Найдено по</dt><dd className="truncate" title={offer.matched_code || offer.article}>{offer.matched_code || offer.article || 'не подтверждено'}</dd><dt className="text-muted-foreground">Проверено</dt><dd>{dateTime(offer.captured_at)}</dd>{offer.quantity !== null && <><dt className="text-muted-foreground">Количество</dt><dd>{offer.quantity}</dd></>}{offer.delivery_text && <><dt className="text-muted-foreground">Доставка</dt><dd>{offer.delivery_text}</dd></>}</dl>
                {offer.match_reasons.length > 0 && <p className="mt-3 line-clamp-2 text-xs text-muted-foreground">{offer.match_reasons.join(' · ')}</p>}
                <div className="mt-4 flex flex-wrap gap-2"><Button size="sm" variant="outline" asChild><a href={offer.url} target="_blank" rel="noreferrer">Открыть <ExternalLink className="ml-1 h-3.5 w-3.5" /></a></Button><Button size="sm" onClick={() => { onApplyPrice(offer.normalized_price!); toast.success(channelLabel === 'Avito' && channelStatus === 'active' ? 'Цена подготовлена. После сохранения отправим безопасное обновление в Avito.' : `Цена подставлена в карточку ${channelLabel}. Проверьте и сохраните её.`); }} disabled={!offer.normalized_price}>Подставить цену</Button></div>
                <p className="mt-2 text-[11px] text-muted-foreground">Источник поиска: {offer.provider_id || 'не указан'}. Подстановка не публикует изменение автоматически.</p>
              </article>
            ))}
          </div>
        )}
        {filtered.length > 6 && <Button variant="ghost" className="w-full" onClick={() => setExpanded((value) => !value)}>{expanded ? <ChevronUp className="mr-2 h-4 w-4" /> : <ChevronDown className="mr-2 h-4 w-4" />}{expanded ? 'Свернуть предложения' : `Показать ещё ${filtered.length - 6}`}</Button>}
      </section>

      <div className="fixed inset-x-0 bottom-0 z-20 border-t bg-background/95 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur md:hidden">
        <Button onClick={refresh} disabled={refreshingRunId !== null || savingGeography} className="w-full">{refreshingRunId ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}{refreshingRunId ? 'Исследуем рынок' : 'Обновить предложения'}</Button>
      </div>

      <Sheet open={filterOpen} onOpenChange={setFilterOpen}>
        <SheetContent side="bottom" className="rounded-t-2xl pb-[max(1.5rem,env(safe-area-inset-bottom))]">
          <SheetHeader><SheetTitle>Фильтры предложений</SheetTitle></SheetHeader>
          <div className="mt-5"><Filters countries={countries} country={country} setCountry={setCountry} onlyInStock={onlyInStock} setOnlyInStock={setOnlyInStock} onlyNew={onlyNew} setOnlyNew={setOnlyNew} showAnalogues={showAnalogues} setShowAnalogues={setShowAnalogues} totalOffers={comparison.internet_offers.length} /></div>
          <Button className="mt-6 w-full" onClick={() => setFilterOpen(false)}>Показать {filtered.length}</Button>
        </SheetContent>
      </Sheet>
    </div>
  );
}
