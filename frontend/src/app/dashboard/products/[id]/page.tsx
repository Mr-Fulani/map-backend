'use client';

import { useEffect, useState, useRef, useCallback, type RefObject } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { accountApi, productApi, imageApi } from '@/lib/api';
import { OzonOfferPreparationCard } from '@/components/products/OzonOfferPreparation';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Separator } from '@/components/ui/separator';
import { toast } from 'sonner';
import { getPreviousDashboardHref } from '@/lib/navigation-history';
import {
  ArrowLeft,
  RefreshCw,
  Upload,
  Loader2,
  Package,
  ImageOff,
  Pencil,
  Search,
  Crown,
  Check,
  X,
  Trash2,
  ExternalLink,
  Store,
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  CatalogCategoryPicker,
  type CatalogCategoryOption,
} from '@/components/products/catalog-category-picker';
import {
  PRODUCT_PHYSICAL_FIELDS,
  canonicalPhysicalValueToDisplay,
  effectivePhysicalValueForInput,
  physicalDraftFromProfile,
  physicalDraftToApiPayload,
  type ProductPhysicalDraft,
  type ProductPhysicalFieldKey,
  type ProductPhysicalProfile,
} from '@/lib/product-physical-profile';
import type { MarketplaceAccount } from '@/lib/marketplace-account-types';

function isProductsListHref(value: string | null): value is string {
  return value === '/dashboard/products' || Boolean(value?.startsWith('/dashboard/products?'));
}

interface ProductImage {
  id: number;
  status: string;
  source_id: string | null;
  quality_score: number | null;
  is_primary: boolean;
  position: number;
  url: string;
  thumb_url: string;
  url_source: string | null;
}

interface ImageSearchResult {
  state: 'running' | 'done' | 'failed' | 'reconciliation_required';
  reason_code?: string;
  message?: string;
  saved_count?: number;
  found_count?: number;
  rejected_count?: number;
  eligible_count?: number;
  download_failed_count?: number;
  sources?: string[];
}

interface ProductDetail {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  brand_resolution_status: string;
  brand_confidence: number;
  brand_source_id: string;
  brand_needs_review: boolean;
  category_1c: string | null;
  condition: string;
  price: string;
  stock_qty: number;
  warehouse: string | null;
  export_enabled: boolean;
  sync_at: string | null;
  created_at: string;
  updated_at: string;
  title_ai: string;
  description_ai: string;
  attributes: ProductAttribute[];
  cross_codes: ProductCrossCode[];
  fitments: VehicleFitment[];
  enrichment_facts: ProductEnrichmentFact[];
  latest_parse_job: ProductParseJob | null;
  parse_jobs_summary: ProductParseJob[];
  listing_options: Array<{
    id: number;
    status: string;
    status_display: string;
    account_name: string;
    title: string;
  }>;
  catalog_category: Pick<
    CatalogCategoryOption,
    'id' | 'name' | 'domain' | 'is_active'
  > | null;
  catalog_classification: ProductCatalogClassification | null;
  physical_profile: ProductPhysicalProfile;
}

interface ProductCatalogClassification {
  domain: string;
  confidence: number;
  source: 'rules' | 'manual' | 'ai';
  reason: string;
  needs_review: boolean;
  review_status: ReviewStatus;
}

interface ProductAttribute {
  id: number;
  source_id: string;
  name: string;
  value: string;
}

interface ProductCrossCode {
  id: number;
  manufacturer: string;
  code: string;
  code_type: string;
}

function buyerFacingCatalogCodes(codes: ProductCrossCode[]): ProductCrossCode[] {
  const result: ProductCrossCode[] = [];
  const positions = new Map<string, number>();
  for (const item of codes) {
    const manufacturer = item.manufacturer.trim().toUpperCase();
    const normalized = item.code.toUpperCase().replace(/[^A-Z0-9]/g, '');
    const identity = manufacturer.includes('MERCEDES') && /^A\d+$/.test(normalized)
      ? normalized.slice(1)
      : normalized;
    const key = `${manufacturer}:${identity}`;
    const position = positions.get(key);
    if (position === undefined) {
      positions.set(key, result.length);
      result.push(item);
    } else if (/^A\d+$/.test(normalized)) {
      const currentNormalized = result[position].code.toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (!/^A\d+$/.test(currentNormalized)) result[position] = item;
    }
  }
  return result;
}

interface VehicleFitment {
  id: number;
  source_id: string;
  source_url: string;
  make: string;
  model: string;
  generation: string;
  date_from: string;
  date_to: string;
  modification: string;
  engine_code: string;
  power_hp: number | null;
  raw_text: string;
  confidence: number;
  needs_review: boolean;
  review_status: ReviewStatus;
}

interface ProductEnrichmentFact {
  id: number;
  source_id: string;
  source_label: string;
  source_url: string;
  fact_type: string;
  name: string;
  value: string;
  confidence: number;
  needs_review: boolean;
  review_status: ReviewStatus;
  created_at: string;
  updated_at: string;
}

type ReviewStatus = 'pending' | 'approved' | 'rejected';

interface ProductParseJob {
  id: number;
  status: string;
  source_id: string;
  source_label: string;
  source_url: string;
  error_message: string;
  parsed_data: {
    image_urls?: string[];
    image_processing?: {
      state: 'completed';
      found_count: number;
      saved_count: number;
      error: string;
    };
  } | null;
  source_offer: {
    price: string | null;
    currency: string;
    price_is_from: boolean;
    availability: 'unknown' | 'in_stock' | 'preorder' | 'out_of_stock';
    availability_label: string;
    availability_text: string;
    quantity: number | null;
    checked_at: string;
  };
  price_comparison: {
    direction: 'tenant_higher' | 'tenant_lower' | 'equal';
    amount: string;
    percent: string;
    tenant_price: string;
    source_price: string;
  } | null;
  created_at: string;
  finished_at: string | null;
}

interface WebResearchEvidence {
  id: number;
  title: string;
  url: string;
  domain: string;
  rank: number;
}

interface WebResearchRun {
  id: number;
  status: string;
  trigger: string;
  search_provider: string;
  ai_provider: string;
  ai_model: string;
  result_count: number;
  claim_count: number;
  generate_after: boolean;
  error_message: string;
  created_at: string;
  finished_at: string | null;
  evidence: WebResearchEvidence[];
}

const CONDITION_LABELS: Record<string, string> = {
  new: 'Новый',
  used: 'Б/у',
  refurbished: 'Восстановленный',
};

const BRAND_STATUS_LABELS: Record<string, string> = {
  unknown: 'Не определён',
  source: 'Из источника',
  catalog: 'Найден в каталоге',
  manual: 'Подтверждён вручную',
  ambiguous: 'Требует проверки',
};

const IMAGE_STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  auto_approved: 'default',
  manually_set: 'default',
  needs_review: 'secondary',
  low_confidence: 'outline',
  rejected: 'destructive',
};

const IMAGE_STATUS_LABELS: Record<string, string> = {
  auto_approved: 'Одобрено',
  manually_set: 'Вручную',
  needs_review: 'На проверке',
  low_confidence: 'Низкое качество',
  rejected: 'Отклонено',
};

const ENRICHMENT_SOURCE_IDS = ['tachka', 'rossko', 'euroauto'] as const;

const ENRICHMENT_SOURCE_LABELS: Record<string, string> = {
  tachka: 'Тачка.ру',
  rossko: 'Росско',
  euroauto: 'Euroauto',
  web_research: 'Интернет-исследование',
  brave: 'Brave',
  tavily: 'Tavily',
  manual: 'Загружено вручную',
  '1c': '1С',
  csv: 'Файл',
};

const ENRICHMENT_FACT_LABELS: Record<string, string> = {
  description: 'Описание из каталога',
  catalog_description: 'Описание из каталога',
  catalog_note: 'Примечание каталога',
};

const WEB_RESEARCH_STATUS_LABELS: Record<string, string> = {
  queued: 'Ожидает запуска',
  running: 'Идёт исследование',
  need_review: 'Найдены данные для проверки',
  completed: 'Проверено',
  no_results: 'Ничего не найдено',
  skipped: 'Не потребовалось',
  failed: 'Ошибка',
};

const CATALOG_DOMAIN_LABELS: Record<string, string> = {
  auto_parts: 'Автозапчасть',
  jewellery: 'Украшение',
  apparel: 'Одежда',
  generic: 'Обычный товар',
  unknown: 'Не определено',
};

const REVIEW_STATUS_LABELS: Record<ReviewStatus, string> = {
  pending: 'Ожидает проверки',
  approved: 'Одобрено',
  rejected: 'Отклонено',
};

function reviewStatusLabel(status: ReviewStatus, needsReview: boolean) {
  if (status === 'pending' && !needsReview) {
    return 'Определено автоматически';
  }
  return REVIEW_STATUS_LABELS[status];
}

function classificationStatusLabel(classification: ProductCatalogClassification) {
  if (classification.source === 'manual' && classification.review_status !== 'rejected') {
    return 'Выбрано вручную';
  }
  return reviewStatusLabel(classification.review_status, classification.needs_review);
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-2">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-right text-sm font-medium">{value ?? '—'}</span>
    </div>
  );
}

function formatRubles(value: string | number) {
  return `${Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽`;
}

function SourcePriceCard({
  sourceId,
  job,
}: {
  sourceId: string;
  job: ProductParseJob | null;
}) {
  const label = job?.source_label || ENRICHMENT_SOURCE_LABELS[sourceId] || sourceId;
  const found = job && ['success', 'need_review'].includes(job.status);
  const checking = job && ['pending', 'running'].includes(job.status);
  const offer = job?.source_offer;

  let statusLabel = 'Не проверялось';
  let statusClass = 'border-border text-muted-foreground';
  if (checking) {
    statusLabel = 'Проверяем…';
  } else if (job?.status === 'not_found') {
    statusLabel = 'Товар не найден';
  } else if (job?.status === 'failed') {
    statusLabel = 'Не удалось проверить';
    statusClass = 'border-destructive/30 text-destructive';
  } else if (found && offer?.availability === 'in_stock') {
    statusLabel = offer.quantity != null ? `В наличии · ${offer.quantity} шт.` : 'В наличии';
    statusClass = 'border-emerald-500/30 text-emerald-700 dark:text-emerald-400';
  } else if (found && offer?.availability === 'preorder') {
    statusLabel = 'Под заказ';
    statusClass = 'border-amber-500/30 text-amber-700 dark:text-amber-400';
  } else if (found && offer?.availability === 'out_of_stock') {
    statusLabel = 'Нет в наличии';
  } else if (found) {
    statusLabel = 'Товар найден';
  }

  let comparisonText = '';
  let comparisonClass = 'text-muted-foreground';
  if (job?.price_comparison?.direction === 'tenant_higher') {
    comparisonText = `Ваша цена выше на ${formatRubles(job.price_comparison.amount)} (${job.price_comparison.percent}%)`;
    comparisonClass = 'text-amber-700 dark:text-amber-400';
  } else if (job?.price_comparison?.direction === 'tenant_lower') {
    comparisonText = `Ваша цена ниже на ${formatRubles(job.price_comparison.amount)} (${job.price_comparison.percent}%)`;
    comparisonClass = 'text-emerald-700 dark:text-emerald-400';
  } else if (job?.price_comparison?.direction === 'equal') {
    comparisonText = 'Цена совпадает с вашей';
  }

  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="flex flex-col items-start gap-2 min-[420px]:flex-row min-[420px]:justify-between">
        <span className="min-w-0 break-words font-medium">{label}</span>
        <Badge variant="outline" className={`shrink-0 text-[11px] ${statusClass}`}>
          {statusLabel}
        </Badge>
      </div>
      <div className="mt-3">
        {found && offer?.price ? (
          <p className="text-xl font-semibold tracking-tight">
            {offer.price_is_from && <span className="mr-1 text-sm font-normal text-muted-foreground">от</span>}
            {formatRubles(offer.price)}
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">
            {checking ? 'Получаем цену…' : found ? 'Цена не указана' : 'Нет данных о цене'}
          </p>
        )}
        {comparisonText && (
          <p className={`mt-1 text-xs font-medium ${comparisonClass}`}>{comparisonText}</p>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="min-w-0 break-words">
          {job?.finished_at
            ? `Проверено ${new Date(job.finished_at).toLocaleString('ru-RU')}`
            : checking ? 'Проверка выполняется' : 'Запустите обогащение'}
        </span>
        {job?.source_url && (
          <a
            href={job.source_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex shrink-0 items-center gap-1 text-primary hover:underline"
          >
            Открыть <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>
    </div>
  );
}

export default function ProductDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const returnTo = searchParams.get('returnTo');
  const [catalogHref, setCatalogHref] = useState('/dashboard/products');
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [catalogCategories, setCatalogCategories] = useState<CatalogCategoryOption[]>([]);
  const [categoryAssignValue, setCategoryAssignValue] = useState('');
  const [categoryAssignLoading, setCategoryAssignLoading] = useState(false);
  const [categoryAssignAction, setCategoryAssignAction] = useState<'apply' | 'remove' | null>(null);
  const [reviewAction, setReviewAction] = useState<string | null>(null);
  const [showAllFitments, setShowAllFitments] = useState(false);
  const [showAllEnrichmentFacts, setShowAllEnrichmentFacts] = useState(false);
  const [pricingListingId, setPricingListingId] = useState<number | null>(null);
  const [marketplaceAccounts, setMarketplaceAccounts] = useState<MarketplaceAccount[]>([]);
  const [ozonPreparationRevision, setOzonPreparationRevision] = useState(0);

  const [images, setImages] = useState<ProductImage[]>([]);
  const [imagesLoading, setImagesLoading] = useState(true);
  const [imageActionId, setImageActionId] = useState<number | null>(null);

  const [editingBrand, setEditingBrand] = useState(false);
  const [brandValue, setBrandValue] = useState('');
  const [savingBrand, setSavingBrand] = useState(false);
  const [physicalDraft, setPhysicalDraft] = useState<ProductPhysicalDraft>(() => (
    physicalDraftFromProfile(null)
  ));
  const [savingPhysicalProfile, setSavingPhysicalProfile] = useState(false);
  const [physicalSuggestionActionId, setPhysicalSuggestionActionId] = useState<number | null>(null);

  const [searchTaskId, setSearchTaskId] = useState<string | null>(null);
  const [imageSearchResult, setImageSearchResult] = useState<ImageSearchResult | null>(null);

  useEffect(() => {
    const previousHref = getPreviousDashboardHref();
    const nextHref = isProductsListHref(previousHref)
      ? previousHref
      : isProductsListHref(returnTo) ? returnTo : null;
    if (!nextHref) return;

    const frame = window.requestAnimationFrame(() => setCatalogHref(nextHref));
    return () => window.cancelAnimationFrame(frame);
  }, [returnTo]);
  const searching = searchTaskId !== null;
  const [generatingDescription, setGeneratingDescription] = useState(false);
  const [parseJobIds, setParseJobIds] = useState<number[]>([]);
  const [primaryParseJobId, setPrimaryParseJobId] = useState<number | null>(null);
  const [parseThenGenerate, setParseThenGenerate] = useState(false);
  const enriching = parseJobIds.length > 0;
  const [webResearch, setWebResearch] = useState<WebResearchRun | null>(null);
  const [webResearchRunId, setWebResearchRunId] = useState<number | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const enrichmentSectionRef = useRef<HTMLDivElement>(null);
  const imagesSectionRef = useRef<HTMLDivElement>(null);
  const descriptionPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [previewImg, setPreviewImg] = useState<string | null>(null);

  const scrollToSection = useCallback((sectionRef: RefObject<HTMLDivElement | null>) => {
    window.requestAnimationFrame(() => {
      sectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }, []);

  const loadProduct = useCallback(async () => {
    const res = await productApi.get(Number(id));
    const nextProduct = res.data.data as ProductDetail;
    setProduct(nextProduct);
    setPhysicalDraft(physicalDraftFromProfile(nextProduct.physical_profile));
    setPricingListingId((current) => (
      nextProduct.listing_options.some((listing) => listing.id === current)
        ? current
        : nextProduct.listing_options[0]?.id ?? null
    ));
    setCategoryAssignValue(nextProduct.catalog_category?.id ? String(nextProduct.catalog_category.id) : '');
  }, [id]);

  const refreshOzonPreparations = useCallback(async () => {
    const accountIds = marketplaceAccounts
      .filter((account) => account.marketplace === 'ozon' && account.is_active)
      .map((account) => account.id);
    if (accountIds.length === 0) return;
    await Promise.allSettled(
      accountIds.map((accountId) => productApi.autofillOzonOffer(Number(id), accountId)),
    );
    setOzonPreparationRevision((current) => current + 1);
  }, [id, marketplaceAccounts]);

  const loadWebResearch = useCallback(async () => {
    const res = await productApi.latestWebResearch(Number(id));
    const run = (res.data.data ?? null) as WebResearchRun | null;
    setWebResearch(run);
    if (run && ['queued', 'running'].includes(run.status)) {
      setWebResearchRunId(run.id);
    }
    return run;
  }, [id]);

  const waitForGeneratedDescription = useCallback((previousDescription: string) => {
    if (descriptionPollRef.current) {
      clearInterval(descriptionPollRef.current);
    }
    setGeneratingDescription(true);
    const deadline = Date.now() + 60_000;
    descriptionPollRef.current = setInterval(async () => {
      if (Date.now() > deadline) {
        if (descriptionPollRef.current) clearInterval(descriptionPollRef.current);
        descriptionPollRef.current = null;
        setGeneratingDescription(false);
        setParseThenGenerate(false);
        toast.warning('Генерация заняла слишком долго. Обновите страницу вручную.');
        return;
      }
      try {
        const productRes = await productApi.get(Number(id));
        const updated = productRes.data.data as ProductDetail;
        if (updated.description_ai && updated.description_ai !== previousDescription) {
          if (descriptionPollRef.current) clearInterval(descriptionPollRef.current);
          descriptionPollRef.current = null;
          setGeneratingDescription(false);
          setParseThenGenerate(false);
          setProduct(updated);
          await refreshOzonPreparations();
          toast.success('Описание сгенерировано на основе доступных данных');
        }
      } catch {
        if (descriptionPollRef.current) clearInterval(descriptionPollRef.current);
        descriptionPollRef.current = null;
        setGeneratingDescription(false);
        setParseThenGenerate(false);
      }
    }, 2000);
  }, [id, refreshOzonPreparations]);

  useEffect(() => () => {
    if (descriptionPollRef.current) clearInterval(descriptionPollRef.current);
  }, []);

  async function saveBrand() {
    setSavingBrand(true);
    try {
      await productApi.updateBrand(Number(id), brandValue.trim());
      await loadProduct();
      await refreshOzonPreparations();
      setEditingBrand(false);
      toast.success('Бренд сохранён — импорт из источника его не перезапишет');
    } catch {
      toast.error('Не удалось сохранить бренд');
    } finally {
      setSavingBrand(false);
    }
  }

  function setPhysicalDraftField(field: ProductPhysicalFieldKey, value: string) {
    setPhysicalDraft((current) => ({ ...current, [field]: value }));
  }

  async function savePhysicalProfile() {
    setSavingPhysicalProfile(true);
    try {
      const payload = physicalDraftToApiPayload(physicalDraft);
      const response = await productApi.updatePhysicalProfile(Number(id), payload);
      const physicalProfile = response.data.data as ProductPhysicalProfile;
      setProduct((current) => current ? { ...current, physical_profile: physicalProfile } : current);
      setPhysicalDraft(physicalDraftFromProfile(physicalProfile));
      await refreshOzonPreparations();
      toast.success('Данные MAP сохранены');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Не удалось сохранить данные товара');
    } finally {
      setSavingPhysicalProfile(false);
    }
  }

  async function reviewPhysicalSuggestion(
    suggestionId: number,
    action: 'approve' | 'reject',
  ) {
    setPhysicalSuggestionActionId(suggestionId);
    try {
      const response = await productApi.reviewPhysicalSuggestion(
        Number(id),
        suggestionId,
        action,
      );
      const physicalProfile = response.data.data as ProductPhysicalProfile;
      setProduct((current) => current ? { ...current, physical_profile: physicalProfile } : current);
      setPhysicalDraft(physicalDraftFromProfile(physicalProfile));
      toast.success(action === 'approve' ? 'Значение подтверждено и записано в MAP' : 'Вариант отклонён');
    } catch (error) {
      const code = (error as { response?: { data?: { code?: string } } })
        .response?.data?.code;
      toast.error(
        code === 'source_value_preferred'
          ? 'Поле уже заполнено корректным значением из 1С.'
          : 'Не удалось сохранить решение. Обновите страницу и повторите.',
      );
    } finally {
      setPhysicalSuggestionActionId(null);
    }
  }

  useEffect(() => {
    let active = true;

    productApi.get(Number(id))
      .then((response) => {
        if (!active) return;
        const nextProduct = response.data.data as ProductDetail;
        setProduct(nextProduct);
        setPhysicalDraft(physicalDraftFromProfile(nextProduct.physical_profile));
        setPricingListingId((current) => (
          nextProduct.listing_options.some((listing) => listing.id === current)
            ? current
            : nextProduct.listing_options[0]?.id ?? null
        ));
        setCategoryAssignValue(
          nextProduct.catalog_category?.id ? String(nextProduct.catalog_category.id) : '',
        );
      })
      .catch(() => {
        if (active) toast.error('Товар не найден');
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    productApi.latestWebResearch(Number(id))
      .then((response) => {
        if (!active) return;
        const run = (response.data.data ?? null) as WebResearchRun | null;
        setWebResearch(run);
        if (run && ['queued', 'running'].includes(run.status)) {
          setWebResearchRunId(run.id);
        }
      })
      .catch(() => undefined);

    accountApi.list()
      .then((response) => {
        if (!active) return;
        setMarketplaceAccounts(
          (response.data.data ?? response.data) as MarketplaceAccount[],
        );
      })
      .catch(() => undefined);

    return () => { active = false; };
  }, [id]);

  useEffect(() => {
    if (!webResearchRunId) return;
    const interval = setInterval(async () => {
      try {
        const res = await productApi.webResearchStatus(webResearchRunId);
        const run = res.data.data as WebResearchRun;
        setWebResearch(run);
        if (!['queued', 'running'].includes(run.status)) {
          const previousDescription = product?.description_ai ?? '';
          setWebResearchRunId(null);
          clearInterval(interval);
          await loadProduct();
          await refreshOzonPreparations();
          if (run.status === 'need_review') {
            toast.warning(`Интернет-агент нашёл факты: ${run.claim_count}. Проверьте их перед применением.`);
          } else if (run.status === 'no_results') {
            toast.warning('Интернет-исследование не нашло подтверждённых данных.');
            if (run.generate_after) {
              toast.info('Генерируем описание из уже подтверждённых данных...');
              waitForGeneratedDescription(previousDescription);
            }
          } else if (run.status === 'failed') {
            toast.error(run.error_message || 'Интернет-исследование завершилось с ошибкой.');
            if (run.generate_after) {
              toast.info('Продолжаем генерацию из уже подтверждённых данных...');
              waitForGeneratedDescription(previousDescription);
            }
          }
        }
      } catch {
        setWebResearchRunId(null);
        clearInterval(interval);
        toast.error('Не удалось получить статус интернет-исследования.');
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [
    webResearchRunId,
    loadProduct,
    product?.description_ai,
    refreshOzonPreparations,
    waitForGeneratedDescription,
  ]);

  useEffect(() => {
    productApi.catalogCategories({ assignable: true })
      .then((res) => setCatalogCategories(res.data.data ?? []))
      .catch(() => toast.error('Не удалось загрузить категории каталога'));
  }, []);

  const loadImages = useCallback(async () => {
    setImagesLoading(true);
    try {
      const res = await imageApi.list(Number(id));
      setImages(res.data.data);
    } catch {
      // ignore
    } finally {
      setImagesLoading(false);
    }
  }, [id]);

  useEffect(() => {
    let active = true;
    imageApi.list(Number(id))
      .then((response) => {
        if (active) setImages(response.data.data);
      })
      .catch(() => undefined)
      .finally(() => {
        if (active) setImagesLoading(false);
      });

    return () => { active = false; };
  }, [id]);

  // Polling статуса поиска каждые 2с
  useEffect(() => {
    if (!searchTaskId) return;
    const interval = setInterval(async () => {
      try {
        const res = await imageApi.searchStatus(Number(id), searchTaskId);
        const result = res.data.data as ImageSearchResult;
        const { state, saved_count = 0 } = result;
        if (state !== 'running') {
          setSearchTaskId(null);
          setImageSearchResult(result);
          if (state === 'done') {
            if (saved_count > 0) {
              toast.success(result.message || `Сохранено фото: ${saved_count}`);
            } else {
              toast.warning(result.message || 'Подходящие фотографии не найдены.');
            }
            loadImages();
          } else {
            toast.error(result.message || 'Поиск завершился с ошибкой');
          }
        }
      } catch {
        setSearchTaskId(null);
        toast.error('Ошибка при опросе статуса');
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [searchTaskId, id, loadImages]);

  useEffect(() => {
    if (parseJobIds.length === 0) return;
    const prevDescription = product?.description_ai ?? '';
    const deadline = Date.now() + 90_000;
    const observedTerminalJobs = new Set<number>();
    const interval = setInterval(async () => {
      if (Date.now() > deadline) {
        setParseJobIds([]);
        setPrimaryParseJobId(null);
        setParseThenGenerate(false);
        clearInterval(interval);
        toast.warning('Обогащение заняло слишком долго. Проверьте очередь задач или попробуйте запустить снова.');
        return;
      }

      try {
        const responses = await Promise.all(
          parseJobIds.map((jobId) => productApi.parseJobStatus(jobId)),
        );
        const jobs = responses.map((response) => response.data.data as ProductParseJob);
        const terminalJobs = jobs.filter((job) => !['pending', 'running'].includes(job.status));
        const settledJobs = terminalJobs.filter((job) => {
          const imageUrls = job.parsed_data?.image_urls ?? [];
          return imageUrls.length === 0
            || job.parsed_data?.image_processing?.state === 'completed';
        });
        const newlyFinishedJobs = settledJobs.filter((job) => !observedTerminalJobs.has(job.id));

        if (newlyFinishedJobs.length > 0) {
          newlyFinishedJobs.forEach((job) => observedTerminalJobs.add(job.id));
          await Promise.all([loadProduct(), loadImages()]);
        }

        if (settledJobs.length === jobs.length) {
          setParseJobIds([]);
          setPrimaryParseJobId(null);
          await refreshOzonPreparations();
          const job = jobs.find((item) => item.id === primaryParseJobId) ?? jobs[0];
          if (parseThenGenerate) {
            // The parser task schedules the fallback immediately before it exits;
            // give the worker a short moment to create the corresponding run.
            await new Promise((resolve) => setTimeout(resolve, 750));
            const research = await loadWebResearch();
            const belongsToCurrentPipeline = research
              && new Date(research.created_at).getTime() >= new Date(job.created_at).getTime();
            if (
              belongsToCurrentPipeline
              && research
              && ['queued', 'running'].includes(research.status)
            ) {
              setParseThenGenerate(false);
              toast.info('Каталоги проверены. Интернет-агент ищет недостающие данные...');
              return;
            }
            if (
              belongsToCurrentPipeline
              && research?.status === 'need_review'
              && research.generate_after
            ) {
              setParseThenGenerate(false);
              toast.warning('Интернет-агент нашёл факты. Подтвердите их перед генерацией описания.');
              return;
            }
            toast.info('Обогащение завершено, генерируем описание...');
            waitForGeneratedDescription(prevDescription);
            return;
          }
          if (jobs.some((item) => item.status === 'success')) {
            toast.success('Данные товара обогащены');
          } else if (jobs.some((item) => item.status === 'need_review')) {
            toast.warning('Данные частично найдены. Проверьте блок «Обогащение данных» в карточке товара.');
          } else if (jobs.every((item) => item.status === 'not_found')) {
            toast.warning('Каталоги не нашли этот товар');
          } else {
            const failedJob = jobs.find((item) => item.status === 'failed');
            toast.error(failedJob?.error_message || 'Обогащение завершилось с ошибкой');
          }
        }
      } catch {
        setParseJobIds([]);
        setPrimaryParseJobId(null);
        setParseThenGenerate(false);
        toast.error('Ошибка при проверке статуса обогащения');
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [
    parseJobIds,
    primaryParseJobId,
    parseThenGenerate,
    product?.description_ai,
    loadImages,
    loadProduct,
    loadWebResearch,
    refreshOzonPreparations,
    waitForGeneratedDescription,
  ]);

  async function startEnrichment(generateAfter = false) {
    setActionLoading(generateAfter ? 'enrich-generate' : 'enrich');
    scrollToSection(enrichmentSectionRef);
    try {
      const res = await productApi.parse(Number(id), '', generateAfter);
      const primaryJobId = Number(res.data.data.job_id);
      const jobIds = Array.isArray(res.data.data.job_ids)
        ? res.data.data.job_ids
          .map((value: unknown) => Number(value))
          .filter((value: number) => Number.isFinite(value))
        : [primaryJobId];
      setParseJobIds(jobIds.length > 0 ? jobIds : [primaryJobId]);
      setPrimaryParseJobId(primaryJobId);
      setParseThenGenerate(generateAfter);
      toast.info(generateAfter ? 'Запущено: обогащение, затем генерация описания' : 'Обогащение запущено');
    } catch (err: unknown) {
      const responseData = (err as { response?: { data?: { code?: string; message?: string } } })?.response?.data;
      const message = responseData?.message;
      toast.error(message ?? (generateAfter ? 'Не удалось запустить подготовку описания' : 'Не удалось запустить обогащение'));
    } finally {
      setActionLoading(null);
    }
  }

  async function startWebResearch() {
    setActionLoading('web-research');
    try {
      const res = await productApi.startWebResearch(Number(id));
      const run = res.data.data as WebResearchRun;
      setWebResearch(run);
      setWebResearchRunId(run.id);
      toast.info('Интернет-исследование запущено');
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string; detail?: string } } }
      ).response?.data;
      toast.error(message?.message ?? message?.detail ?? 'Не удалось запустить интернет-исследование');
    } finally {
      setActionLoading(null);
    }
  }

  async function assignCatalogCategory(categoryId: number | null) {
    setCategoryAssignLoading(true);
    setCategoryAssignAction(categoryId === null ? 'remove' : 'apply');
    try {
      await productApi.assignCatalogCategory({
        product_ids: [Number(id)],
        catalog_category: categoryId,
      });
      if (categoryId === null) {
        setCategoryAssignValue('');
        setProduct((current) => current ? {...current, catalog_category: null} : current);
      }
      await loadProduct();
      toast.success(categoryId ? 'Категория товара обновлена' : 'Категория товара снята');
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось изменить категорию товара');
    } finally {
      setCategoryAssignLoading(false);
      setCategoryAssignAction(null);
    }
  }

  async function handleReview(
    target: 'classification' | 'fitment' | 'fact',
    action: 'approve' | 'reject',
    itemId?: number,
  ) {
    const key = `${target}-${itemId ?? 'main'}-${action}`;
    const previousDescription = product?.description_ai ?? '';
    setReviewAction(key);
    try {
      if (target === 'classification') {
        await productApi.reviewCatalogClassification(Number(id), action);
      } else if (target === 'fitment' && itemId) {
        await productApi.reviewFitment(Number(id), itemId, action);
      } else if (target === 'fact' && itemId) {
        await productApi.reviewEnrichmentFact(Number(id), itemId, action);
      }
      await loadProduct();
      const research = await loadWebResearch();
      if (research?.status === 'completed' && research.generate_after) {
        toast.info('Все найденные факты проверены. Генерируем описание...');
        waitForGeneratedDescription(previousDescription);
      }
      toast.success(action === 'approve' ? 'Данные одобрены' : 'Данные отклонены');
    } catch {
      toast.error('Не удалось сохранить проверку');
    } finally {
      setReviewAction(null);
    }
  }

  async function startSearch() {
    setActionLoading('search');
    setImageSearchResult(null);
    scrollToSection(imagesSectionRef);
    try {
      const res = await imageApi.search(Number(id));
      setSearchTaskId(res.data.data.task_id);
      toast.info('Поиск фотографий запущен');
    } catch {
      toast.error('Не удалось запустить поиск');
    } finally {
      setActionLoading(null);
    }
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = '';
    setActionLoading('upload');
    try {
      await imageApi.upload(Number(id), file);
      toast.success('Фото загружено');
      loadImages();
    } catch {
      toast.error('Ошибка загрузки фото');
    } finally {
      setActionLoading(null);
    }
  }

  async function handleImageAction(
    imageId: number,
    action: 'approve' | 'reject' | 'setPrimary' | 'delete',
  ) {
    setImageActionId(imageId);
    try {
      if (action === 'approve') await imageApi.approve(Number(id), imageId);
      else if (action === 'reject') await imageApi.reject(Number(id), imageId);
      else if (action === 'setPrimary') await imageApi.setPrimary(Number(id), imageId);
      else await imageApi.delete(Number(id), imageId);
      loadImages();
    } catch {
      toast.error('Ошибка');
    } finally {
      setImageActionId(null);
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <div className="grid gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2 space-y-4">
            <Skeleton className="h-48 w-full rounded-xl" />
            <Skeleton className="h-32 w-full rounded-xl" />
          </div>
          <Skeleton className="h-64 w-full rounded-xl" />
        </div>
      </div>
    );
  }

  if (!product) {
    return (
      <div className="flex flex-col items-center gap-4 py-24 text-center">
        <Package className="h-12 w-12 text-muted-foreground/30" />
        <p className="text-muted-foreground">Товар не найден</p>
        <Button variant="outline" onClick={() => router.back()}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>
      </div>
    );
  }

  // Фоновое интернет-исследование не должно блокировать остальные действия
  // с товаром. Конкретные операции ниже отдельно учитывают поиск изображений,
  // обогащение и генерацию описания.
  const busy = actionLoading !== null;
  const assignedCatalogCategoryValue = product.catalog_category?.id
    ? String(product.catalog_category.id)
    : '';
  const catalogCategoryChanged = Boolean(categoryAssignValue)
    && categoryAssignValue !== assignedCatalogCategoryValue;
  const descriptionAwaitsResearchReview = webResearch?.status === 'need_review'
    && webResearch.generate_after;
  const physicalProfile = product.physical_profile;
  const requiredPhysicalMissingFields = physicalProfile.missing_fields.filter(
    (field) => field !== 'vat_rate',
  );

  return (
    <div className="space-y-6">
      {/* Навигация */}
      <div className="flex min-w-0 items-center gap-2 sm:gap-3">
        <Link href={catalogHref}>
          <Button variant="ghost" size="sm">
            <ArrowLeft className="mr-2 h-4 w-4" />
            Каталог
          </Button>
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="min-w-0 truncate font-mono text-sm" title={product.article}>{product.article}</span>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Основная информация + Фото */}
        <div className="space-y-6 lg:col-span-2">
          <Card>
            <CardHeader className="flex flex-col items-start gap-3 min-[480px]:flex-row min-[480px]:justify-between">
              <div className="min-w-0">
                <CardTitle className="break-words text-xl">{product.name}</CardTitle>
                <p className="mt-1 break-all font-mono text-sm text-muted-foreground">{product.article}</p>
              </div>
              <Badge variant={product.export_enabled ? 'default' : 'secondary'} className="shrink-0">
                {product.export_enabled ? 'Выгружается' : 'Не выгружается'}
              </Badge>
            </CardHeader>
            <CardContent>
              <Separator className="mb-4" />
              <div className="divide-y">
                <div className="flex flex-col gap-2 py-2 min-[480px]:flex-row min-[480px]:items-center min-[480px]:justify-between min-[480px]:gap-4">
                  <span className="text-sm text-muted-foreground">Бренд</span>
                  {editingBrand ? (
                    <div className="flex min-w-0 w-full items-center gap-2 min-[480px]:w-auto">
                      <Input
                        className="h-8 min-w-0 flex-1 min-[480px]:w-48 min-[480px]:flex-none"
                        value={brandValue}
                        onChange={(e) => setBrandValue(e.target.value)}
                        placeholder="Например: Hyundai-KIA"
                        disabled={savingBrand}
                        autoFocus
                      />
                      <Button size="sm" className="h-8" onClick={saveBrand} disabled={savingBrand}>
                        {savingBrand ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8"
                        onClick={() => setEditingBrand(false)}
                        disabled={savingBrand}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  ) : (
                    <span className="flex min-w-0 flex-wrap items-center gap-2 text-sm font-medium min-[480px]:justify-end min-[480px]:text-right">
                      <span className="min-w-0 break-words">{product.brand || '—'}</span>
                      <Badge
                        variant={
                          product.brand_needs_review
                            ? 'destructive'
                            : product.brand_resolution_status === 'manual'
                              ? 'default'
                              : 'outline'
                        }
                        title={[
                          product.brand_source_id && `Источник: ${product.brand_source_id}`,
                          product.brand_confidence > 0
                            && `Уверенность: ${Math.round(product.brand_confidence * 100)}%`,
                        ].filter(Boolean).join(' · ')}
                      >
                        {BRAND_STATUS_LABELS[product.brand_resolution_status]
                          ?? product.brand_resolution_status}
                      </Badge>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 w-7 p-0"
                        title="Изменить бренд (нужен для публикации на Avito)"
                        onClick={() => { setBrandValue(product.brand || ''); setEditingBrand(true); }}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                    </span>
                  )}
                </div>
                <Field label="Категория 1С" value={product.category_1c} />
                <Field label="Состояние" value={CONDITION_LABELS[product.condition] ?? product.condition} />
                <Field label="Склад" value={product.warehouse} />
                <Field
                  label="Цена"
                  value={
                    <span className="text-lg font-bold">
                      {Number(product.price).toLocaleString('ru-RU')} ₽
                    </span>
                  }
                />
                <Field
                  label="Остаток"
                  value={
                    <span className={product.stock_qty === 0 ? 'text-destructive' : ''}>
                      {product.stock_qty} шт.
                    </span>
                  }
                />
                <Field
                  label="Последняя синхронизация"
                  value={
                    product.sync_at
                      ? new Date(product.sync_at).toLocaleString('ru-RU')
                      : '—'
                  }
                />
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="text-base">Упаковка и налог</CardTitle>
                <Badge variant={requiredPhysicalMissingFields.length === 0 ? 'default' : 'outline'}>
                  {requiredPhysicalMissingFields.length === 0
                    ? 'Обязательные данные заполнены'
                    : `Нужно заполнить: ${requiredPhysicalMissingFields.length}`}
                </Badge>
              </div>
              <p className="text-sm leading-relaxed text-muted-foreground">
                Это общие факты о товаре. MAP сначала использует корректное значение из
                1С. Если его нет или 1С передала ошибку, заполните поле здесь. Значения
                пригодятся маркетплейсам с требованиями к упаковке и не меняют поля Avito.
                НДС указывайте только по данным 1С или бухгалтера — неизвестную ставку
                можно оставить пустой.
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                {PRODUCT_PHYSICAL_FIELDS.map(({ key, label, unit, placeholder }) => {
                  const fact = physicalProfile.facts[key];
                  const from1c = fact.effective_source === '1c';
                  const suggestions = physicalProfile.suggestions.filter(
                    (suggestion) => suggestion.field === key,
                  );
                  const value = effectivePhysicalValueForInput(
                    physicalProfile,
                    key,
                    physicalDraft,
                  );
                  return (
                    <div key={key} className="space-y-1.5 rounded-lg border p-3">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <label htmlFor={`physical-${key}`} className="text-sm font-medium">
                            {label}{unit ? `, ${unit}` : ''}
                          </label>
                          {key === 'vat_rate' && <Badge variant="outline">Необязательно</Badge>}
                        </div>
                        <Badge
                          variant={from1c ? 'secondary' : fact.effective_source === 'map' ? 'outline' : 'destructive'}
                          className="shrink-0"
                        >
                          {from1c
                            ? 'Из 1С'
                            : fact.effective_source === 'map' ? 'Заполнено в MAP' : 'Не заполнено'}
                        </Badge>
                      </div>
                      {key === 'vat_rate' ? (
                        <Select
                          value={value || 'not_set'}
                          onValueChange={(next) => setPhysicalDraftField(
                            key,
                            next === 'not_set' ? '' : next,
                          )}
                          disabled={from1c || savingPhysicalProfile}
                        >
                          <SelectTrigger id={`physical-${key}`}>
                            <SelectValue placeholder="Выберите ставку" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="not_set">Не указано</SelectItem>
                            {['0', '5', '7', '10', '20'].map((rate) => (
                              <SelectItem key={rate} value={rate}>{rate}%</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      ) : (
                        <Input
                          id={`physical-${key}`}
                          value={value}
                          onChange={(event) => setPhysicalDraftField(key, event.target.value)}
                          placeholder={placeholder}
                          inputMode={key === 'barcode' ? 'text' : 'decimal'}
                          disabled={from1c || savingPhysicalProfile}
                        />
                      )}
                      {from1c ? (
                        <p className="text-xs text-muted-foreground">
                          Используется автоматически. Значение MAP не перезаписывает 1С.
                        </p>
                      ) : fact.source_error ? (
                        <p className="text-xs text-amber-700 dark:text-amber-400">
                          Значение из 1С не принято: {fact.source_error}
                        </p>
                      ) : key === 'vat_rate' ? (
                        <p className="text-xs text-muted-foreground">
                          Не знаете ставку — оставьте «Не указано». Это не блокирует
                          подготовку карточки Ozon.
                        </p>
                      ) : (
                        <p className="text-xs text-muted-foreground">
                          {fact.effective_source === 'map'
                            ? 'Будет использоваться, пока в 1С нет корректного значения.'
                            : 'Нет корректного значения в 1С или MAP.'}
                        </p>
                      )}
                      {fact.map_provenance && fact.effective_source === 'map' && (
                        <p className="text-xs text-emerald-700 dark:text-emerald-400">
                          Подтверждено из{' '}
                          {fact.map_provenance.source_url ? (
                            <a
                              href={fact.map_provenance.source_url}
                              target="_blank"
                              rel="noreferrer"
                              className="font-medium underline underline-offset-2"
                            >
                              {fact.map_provenance.source_label}
                            </a>
                          ) : fact.map_provenance.source_label}
                          : {fact.map_provenance.raw_value}
                        </p>
                      )}
                      {suggestions.length > 0 && (
                        <div className="space-y-2 rounded-md border bg-muted/30 p-2.5">
                          <p className="text-xs font-medium">Найдено при обогащении</p>
                          {suggestions.map((suggestion) => {
                            const isApplied = fact.map_provenance?.suggestion_id === suggestion.id
                              && fact.effective_source === 'map';
                            const isBusy = physicalSuggestionActionId === suggestion.id;
                            return (
                              <div key={suggestion.id} className="space-y-2 rounded-md bg-background p-2">
                                <div className="flex flex-wrap items-start justify-between gap-2 text-xs">
                                  <div className="min-w-0">
                                    <p className="font-medium">
                                      {canonicalPhysicalValueToDisplay(key, suggestion.value)}
                                      {unit ? ` ${unit}` : ''}
                                    </p>
                                    <p className="break-words text-muted-foreground">
                                      {suggestion.source_url ? (
                                        <a
                                          href={suggestion.source_url}
                                          target="_blank"
                                          rel="noreferrer"
                                          className="underline underline-offset-2"
                                        >
                                          {suggestion.source_label}
                                        </a>
                                      ) : suggestion.source_label}
                                      {' · '}{suggestion.raw_name}: {suggestion.raw_value}
                                    </p>
                                  </div>
                                  {(isApplied || suggestion.review_status === 'rejected') && (
                                    <Badge variant="outline" className="shrink-0">
                                      {isApplied ? 'Использовано' : 'Отклонено'}
                                    </Badge>
                                  )}
                                </div>
                                {!from1c && !isApplied && (
                                  <div className="flex flex-wrap gap-2">
                                    <Button
                                      type="button"
                                      size="sm"
                                      className="h-7"
                                      onClick={() => reviewPhysicalSuggestion(suggestion.id, 'approve')}
                                      disabled={physicalSuggestionActionId !== null || savingPhysicalProfile}
                                    >
                                      {isBusy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                                      Использовать в MAP
                                    </Button>
                                    {suggestion.review_status !== 'rejected' && (
                                      <Button
                                        type="button"
                                        size="sm"
                                        variant="ghost"
                                        className="h-7"
                                        onClick={() => reviewPhysicalSuggestion(suggestion.id, 'reject')}
                                        disabled={physicalSuggestionActionId !== null || savingPhysicalProfile}
                                      >
                                        Отклонить
                                      </Button>
                                    )}
                                  </div>
                                )}
                                {from1c && (
                                  <p className="text-xs text-muted-foreground">
                                    Вариант сохранён для проверки, но используется значение из 1С.
                                  </p>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-xs text-muted-foreground">
                  Размеры вводятся в сантиметрах, вес — в килограммах.
                </p>
                <Button onClick={savePhysicalProfile} disabled={savingPhysicalProfile}>
                  {savingPhysicalProfile && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Сохранить данные MAP
                </Button>
              </div>
            </CardContent>
          </Card>

          {/* AI-описание */}
          {(product.description_ai || generatingDescription) && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium">AI-описание</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {descriptionAwaitsResearchReview && !generatingDescription && (
                  <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
                    <p className="font-medium">Описание ещё не обновлено</p>
                    <p className="mt-1 text-xs leading-relaxed opacity-90">
                      Интернет-поиск нашёл данные, которые требуют проверки. Старое описание
                      остаётся на экране до решения по найденным вариантам; после проверки
                      новое описание сгенерируется автоматически.
                    </p>
                    <Button
                      className="mt-3"
                      size="sm"
                      variant="outline"
                      onClick={() => scrollToSection(enrichmentSectionRef)}
                    >
                      Перейти к проверке ({webResearch.claim_count})
                    </Button>
                  </div>
                )}
                {generatingDescription ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
                    <Loader2 className="h-4 w-4 animate-spin" />
                {enriching && parseThenGenerate ? 'Сначала обогащаем данные...' : 'Генерация описания...'}
                  </div>
                ) : (
                  <>
                    {product.title_ai && (
                      <div>
                        <p className="text-xs text-muted-foreground mb-1">Заголовок</p>
                        <p className="text-sm font-medium">{product.title_ai}</p>
                      </div>
                    )}
                    {product.description_ai && (
                      <div>
                        <p className="text-xs text-muted-foreground mb-1">Описание</p>
                        <p className="text-sm whitespace-pre-wrap leading-relaxed">
                          {product.description_ai}
                        </p>
                      </div>
                    )}
                  </>
                )}
              </CardContent>
            </Card>
          )}

          <Card ref={enrichmentSectionRef} className="scroll-mt-24 overflow-hidden">
            <CardHeader className="border-b bg-muted/30">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <CardTitle className="text-sm font-medium">Обогащение данных</CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Достоверные факты из внешних каталогов для описания, OEM и применяемости.
                  </p>
                </div>
                {webResearch && (
                  <div className="flex flex-wrap gap-1">
                    <Badge
                      variant={webResearch.status === 'need_review' ? 'secondary' : 'outline'}
                      className="text-xs"
                    >
                      Интернет: {WEB_RESEARCH_STATUS_LABELS[webResearch.status] ?? webResearch.status}
                    </Badge>
                  </div>
                )}
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-4 sm:pt-5">
              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                {product.latest_parse_job && (
                  <span>
                    Последний запуск: {new Date(product.latest_parse_job.created_at).toLocaleString('ru-RU')}
                  </span>
                )}
              </div>

              <div>
                <div className="mb-2">
                  <p className="text-sm font-medium">Цены и наличие в источниках</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Сравниваем найденные цены с вашей текущей ценой {formatRubles(product.price)}.
                  </p>
                </div>
                <div className="grid gap-3 lg:grid-cols-3">
                  {ENRICHMENT_SOURCE_IDS.map((sourceId) => (
                    <SourcePriceCard
                      key={sourceId}
                      sourceId={sourceId}
                      job={(product.parse_jobs_summary ?? []).find((item) => item.source_id === sourceId) ?? null}
                    />
                  ))}
                </div>
                <p className="mt-2 text-xs text-muted-foreground">
                  Цены справочные: скидки, доставка и условия конкретного продавца могут отличаться.
                </p>
                <div className="mt-4 rounded-lg border bg-gradient-to-br from-primary/5 via-background to-emerald-500/5 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="max-w-2xl">
                      <p className="text-sm font-medium">Полное сравнение рынка — в листинге</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Больше предложений, типичная цена на рынке и сравнение с ценой объявления
                        доступны в рабочем пространстве конкретного листинга.
                      </p>
                    </div>
                    {product.listing_options.length === 1 && (
                      <Button asChild size="sm" className="shrink-0">
                        <Link href={`/dashboard/listings?listing=${product.listing_options[0].id}&panel=pricing`}>
                          Сравнить цены в листинге
                        </Link>
                      </Button>
                    )}
                  </div>
                  {product.listing_options.length > 1 && (
                    <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                      <select
                        value={pricingListingId ?? ''}
                        onChange={(event) => setPricingListingId(Number(event.target.value))}
                        className="h-9 min-w-0 flex-1 rounded-md border bg-background px-3 text-sm"
                      >
                        {product.listing_options.map((listing) => (
                          <option key={listing.id} value={listing.id}>
                            {listing.account_name} · {listing.status_display}
                          </option>
                        ))}
                      </select>
                      <Button asChild size="sm" disabled={!pricingListingId}>
                        <Link href={`/dashboard/listings?listing=${pricingListingId}&panel=pricing`}>
                          Сравнить выбранный листинг
                        </Link>
                      </Button>
                    </div>
                  )}
                  {product.listing_options.length === 0 && (
                    <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                      <p className="text-xs text-muted-foreground">
                        Сначала выберите маркетплейс и кабинет в разделе «Листинги».
                      </p>
                      <Button asChild size="sm" variant="outline">
                        <Link href={`/dashboard/listings?product=${product.id}`}>
                          Перейти в Листинги
                        </Link>
                      </Button>
                    </div>
                  )}
                </div>
              </div>

              {webResearch && (
                <div className="rounded-md border bg-muted/20 p-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">Интернет-исследование</span>
                    <span className="text-xs text-muted-foreground">
                      Страниц: {webResearch.result_count} · фактов: {webResearch.claim_count}
                    </span>
                  </div>
                  {webResearch.error_message && (
                    <p className="mt-2 text-xs text-destructive">{webResearch.error_message}</p>
                  )}
                  {webResearch.evidence.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                      {webResearch.evidence.slice(0, 6).map((item) => (
                        <a
                          key={item.id}
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                          className="max-w-full truncate text-primary hover:underline"
                          title={item.title || item.url}
                        >
                          {item.domain}
                        </a>
                      ))}
                    </div>
                  )}
                  {['completed', 'no_results', 'failed'].includes(webResearch.status) && (
                    <Button
                      className="mt-3"
                      size="sm"
                      variant="outline"
                      onClick={startWebResearch}
                      disabled={busy || searching || enriching || generatingDescription}
                    >
                      {actionLoading === 'web-research' ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      ) : (
                        <RefreshCw className="mr-2 h-4 w-4" />
                      )}
                      Повторить интернет-поиск
                    </Button>
                  )}
                </div>
              )}

              {(product.parse_jobs_summary ?? (product.latest_parse_job ? [product.latest_parse_job] : []))
                .filter((j) => j.error_message)
                .map((job) => (
                  <details
                    key={job.source_id}
                    className={job.status === 'failed'
                      ? 'rounded-md border border-destructive/20 bg-destructive/5 p-2 text-sm'
                      : 'rounded-md border bg-muted/20 p-2 text-sm'}
                  >
                    <summary className="cursor-pointer font-medium">
                      {job.source_label || ENRICHMENT_SOURCE_LABELS[job.source_id] || job.source_id}:{' '}
                      {job.status === 'not_found' ? 'почему товар не найден' : 'подробности проверки'}
                    </summary>
                    <p className="mt-2 break-words text-xs text-muted-foreground">{job.error_message}</p>
                  </details>
                ))}

              {product.attributes.length === 0
                && product.cross_codes.length === 0
                && product.fitments.length === 0
                && product.enrichment_facts.length === 0 ? (
                <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                  Обогащённые данные пока не сохранены. Запустите обогащение или выберите другой источник.
                </p>
              ) : (
                <div className="space-y-4">
                  {product.attributes.length > 0 && (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                        Характеристики
                      </p>
                      <div className="overflow-hidden rounded-lg border">
                        {product.attributes.slice(0, 8).map((attr) => (
                          <div
                            key={attr.id}
                            className="grid gap-1 px-3 py-2 text-sm sm:grid-cols-[180px_minmax(0,1fr)]"
                          >
                            <span className="text-muted-foreground">{attr.name}</span>
                            <span className="min-w-0 break-words font-medium">{attr.value}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {product.cross_codes.length > 0 && (() => {
                    const catalogCodes = buyerFacingCatalogCodes(product.cross_codes);
                    return (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                        Номера для поиска и проверки совместимости
                      </p>
                      <p className="mb-2 text-xs text-muted-foreground">
                        По этим номерам покупатель может найти деталь или сверить её с VIN.
                        Форматные дубли скрыты.
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {catalogCodes.slice(0, 12).map((cross) => (
                          <Badge key={cross.id} variant="outline" className="max-w-full whitespace-normal break-all px-2 py-1">
                            {cross.manufacturer ? `${cross.manufacturer}: ` : ''}
                            {cross.code}
                          </Badge>
                        ))}
                      </div>
                      {catalogCodes.length > 12 && (
                        <p className="mt-2 text-xs text-muted-foreground">
                          Ещё номеров: {catalogCodes.length - 12}
                        </p>
                      )}
                    </div>
                    );
                  })()}

                  {product.fitments.length > 0 && (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                        Совместимость с автомобилями
                      </p>
                      <p className="mb-2 text-xs text-muted-foreground">
                        MAP берёт варианты только из блока применяемости конкретного товара.
                        Проверьте исходную строку и при необходимости откройте источник. Длинный
                        список допустим: название товара не используется как жёсткий фильтр.
                      </p>
                      <div className="space-y-2">
                        {(showAllFitments ? product.fitments : product.fitments.slice(0, 5)).map((fitment) => {
                          const canReview = fitment.review_status === 'pending';
                          const sourceLabel = ENRICHMENT_SOURCE_LABELS[fitment.source_id]
                            || fitment.source_id
                            || 'Источник';
                          const period = [fitment.date_from, fitment.date_to].filter(Boolean).join('–');
                          return (
                          <div key={fitment.id} className="flex flex-col gap-2 rounded-md border p-3 text-sm sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0 space-y-1">
                              <div>
                                <span className="break-words font-medium">
                                  {[
                                    fitment.make,
                                    fitment.model,
                                    fitment.generation,
                                    period,
                                    fitment.modification,
                                    fitment.engine_code ? `(${fitment.engine_code})` : '',
                                    fitment.power_hp ? `${fitment.power_hp} л.с.` : '',
                                  ].filter(Boolean).join(' ')}
                                </span>
                                <Badge
                                  variant={fitment.review_status === 'rejected' ? 'destructive' : 'secondary'}
                                  className="ml-2"
                                >
                                  {reviewStatusLabel(fitment.review_status, canReview || fitment.needs_review)}
                                </Badge>
                              </div>
                              <p className="break-words text-xs text-muted-foreground">
                                Источник:{' '}
                                {fitment.source_url ? (
                                  <a
                                    href={fitment.source_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="inline-flex items-center gap-1 underline underline-offset-2"
                                  >
                                    {sourceLabel}
                                    <ExternalLink className="h-3 w-3" />
                                  </a>
                                ) : sourceLabel}
                              </p>
                              {fitment.raw_text && (
                                <p className="break-words text-xs text-muted-foreground">
                                  Фрагмент: {fitment.raw_text}
                                </p>
                              )}
                            </div>
                            {canReview && (
                              <div className="flex gap-2">
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => handleReview('fitment', 'approve', fitment.id)}
                                  disabled={reviewAction !== null}
                                >
                                  {reviewAction === `fitment-${fitment.id}-approve` ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                  ) : (
                                    <Check className="h-4 w-4" />
                                  )}
                                  Одобрить
                                </Button>
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => handleReview('fitment', 'reject', fitment.id)}
                                  disabled={reviewAction !== null}
                                >
                                  {reviewAction === `fitment-${fitment.id}-reject` ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                  ) : (
                                    <X className="h-4 w-4" />
                                  )}
                                  Отклонить
                                </Button>
                              </div>
                            )}
                          </div>
                          );
                        })}
                      </div>
                      {product.fitments.length > 5 && (
                        <Button
                          className="mt-2 h-auto px-0 text-xs"
                          type="button"
                          variant="link"
                          onClick={() => setShowAllFitments((current) => !current)}
                        >
                          {showAllFitments
                            ? 'Свернуть список'
                            : `Показать все варианты (${product.fitments.length})`}
                        </Button>
                      )}
                    </div>
                  )}

                  {product.enrichment_facts.length > 0 && (
                    <div>
                      <div className="mb-2">
                        <p className="text-xs font-medium uppercase text-muted-foreground">
                          Данные из источников
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          Используются при создании описания и не публикуются как отдельный текст.
                        </p>
                      </div>
                      <div className="space-y-2">
                        {(showAllEnrichmentFacts
                          ? product.enrichment_facts
                          : product.enrichment_facts.slice(0, 5)).map((fact) => {
                          const factLabel = ENRICHMENT_FACT_LABELS[fact.name] || fact.name;
                          const sourceLabel = fact.source_label
                            || ENRICHMENT_SOURCE_LABELS[fact.source_id]
                            || fact.source_id;
                          const longValue = fact.value.length > 260;
                          return (
                            <div key={fact.id} className="flex flex-col gap-3 rounded-md border p-3 text-sm sm:flex-row sm:items-start sm:justify-between">
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-2">
                                  <p className="font-medium">{factLabel}</p>
                                  <Badge variant="outline" className="text-[11px]">{sourceLabel}</Badge>
                                </div>
                                <p className="mt-1 text-xs text-muted-foreground">
                                  Обновлено {new Date(fact.updated_at || fact.created_at).toLocaleString('ru-RU')}
                                  {fact.source_url && (
                                    <>
                                      {' · '}
                                      <a
                                        href={fact.source_url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="inline-flex items-center gap-1 text-primary hover:underline"
                                      >
                                        Источник <ExternalLink className="h-3 w-3" />
                                      </a>
                                    </>
                                  )}
                                </p>
                                {longValue ? (
                                  <details className="mt-2 rounded-md bg-muted/30 p-2">
                                    <summary className="cursor-pointer text-xs font-medium text-primary">
                                      Показать исходный текст
                                    </summary>
                                    <p className="mt-2 break-words text-xs text-muted-foreground">
                                      {fact.value}
                                    </p>
                                  </details>
                                ) : (
                                  <p className="mt-2 break-words text-muted-foreground">{fact.value}</p>
                                )}
                                <Badge
                                  variant={fact.review_status === 'rejected' ? 'destructive' : 'secondary'}
                                  className="mt-2"
                                >
                                  {reviewStatusLabel(fact.review_status, fact.needs_review)}
                                </Badge>
                              </div>
                              {fact.needs_review && (
                                <div className="flex shrink-0 gap-2">
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    onClick={() => handleReview('fact', 'approve', fact.id)}
                                    disabled={reviewAction !== null}
                                  >
                                    <Check className="h-4 w-4" />
                                    Одобрить
                                  </Button>
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    onClick={() => handleReview('fact', 'reject', fact.id)}
                                    disabled={reviewAction !== null}
                                  >
                                    <X className="h-4 w-4" />
                                    Отклонить
                                  </Button>
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      {product.enrichment_facts.length > 5 && (
                        <Button
                          className="mt-2 h-auto px-0 text-xs"
                          type="button"
                          variant="link"
                          onClick={() => setShowAllEnrichmentFacts((current) => !current)}
                        >
                          {showAllEnrichmentFacts
                            ? 'Свернуть список'
                            : `Показать все данные (${product.enrichment_facts.length})`}
                        </Button>
                      )}
                    </div>
                  )}

                </div>
              )}
            </CardContent>
          </Card>

          {marketplaceAccounts.some((account) => (
            account.marketplace === 'ozon' && account.is_active
          )) && (
            <OzonOfferPreparationCard
              productId={product.id}
              accounts={marketplaceAccounts}
              refreshToken={ozonPreparationRevision}
            />
          )}

          {/* Фотографии */}
          <Card ref={imagesSectionRef} className="scroll-mt-24">
            <CardHeader>
              <CardTitle className="text-sm font-medium">
                Фотографии {images.length > 0 && `(${images.length})`}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(searching || actionLoading === 'search') && (
                <div
                  role="status"
                  className="mb-4 flex items-start gap-2 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-200"
                >
                  <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin" />
                  <div>
                    <p className="font-medium">
                      {searching ? 'Идёт поиск фотографий...' : 'Запускаем поиск фотографий...'}
                    </p>
                    <p className="mt-0.5 text-xs opacity-80">
                      Проверяем Brave и резервный Tavily по OEM, названию и применяемости. Не нажимайте кнопку повторно.
                    </p>
                  </div>
                </div>
              )}
              {!searching && imageSearchResult?.message && (
                <div className="mb-4 rounded-md border bg-muted/30 p-3 text-sm">
                  <p className="font-medium">Результат поиска фотографий</p>
                  <p className="mt-1 text-muted-foreground">{imageSearchResult.message}</p>
                  {(imageSearchResult.found_count ?? 0) > 0 && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      Найдено сервисами: {imageSearchResult.found_count ?? 0}
                      {' · '}прошли проверку товара: {imageSearchResult.eligible_count ?? 0}
                      {' · '}отклонено как нерелевантные: {imageSearchResult.rejected_count ?? 0}
                      {(imageSearchResult.download_failed_count ?? 0) > 0
                        ? ` · не удалось скачать: ${imageSearchResult.download_failed_count}`
                        : ''}
                    </p>
                  )}
                  {(imageSearchResult.sources?.length ?? 0) > 0 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      Источники: {imageSearchResult.sources?.join(' → ')}
                    </p>
                  )}
                </div>
              )}
              {imagesLoading ? (
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                  {Array.from({ length: 4 }).map((_, i) => (
                    <Skeleton key={i} className="aspect-square rounded-lg" />
                  ))}
                </div>
              ) : images.length === 0 ? (
                <div className="flex flex-col items-center gap-2 py-8 text-center text-muted-foreground">
                  <ImageOff className="h-8 w-8 opacity-30" />
                  <p className="text-sm">Фотографии не загружены</p>
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  {images
                    .sort((a, b) => Number(b.is_primary) - Number(a.is_primary) || a.position - b.position)
                    .map((img) => (
                      <div key={img.id} className="space-y-1">
                        <div className="relative aspect-square overflow-hidden rounded-lg border bg-muted">
                          {img.is_primary && (
                            <div className="absolute top-1 left-1 z-10">
                              <Crown className="h-4 w-4 text-yellow-500 drop-shadow" />
                            </div>
                          )}
                          <button
                            type="button"
                            onClick={() => setPreviewImg(img.url || img.thumb_url)}
                            className="w-full h-full"
                          >
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={img.thumb_url || img.url_source || ''}
                              alt=""
                              className="h-full w-full object-cover"
                              loading="lazy"
                            />
                          </button>
                        </div>
                        <Badge
                          variant={IMAGE_STATUS_VARIANTS[img.status] ?? 'outline'}
                          className="text-xs w-full justify-center"
                        >
                          {img.is_primary ? 'Главное • ' : ''}
                          {IMAGE_STATUS_LABELS[img.status] ?? img.status}
                        </Badge>
                        {img.source_id && (
                          <p className="truncate text-center text-[11px] text-muted-foreground">
                            Источник: {ENRICHMENT_SOURCE_LABELS[img.source_id] ?? img.source_id}
                          </p>
                        )}
                        <div className="flex gap-1">
                          {!img.is_primary && (
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 flex-1"
                              title="Сделать главным"
                              disabled={imageActionId === img.id}
                              onClick={() => handleImageAction(img.id, 'setPrimary')}
                            >
                              <Crown className="h-3.5 w-3.5" />
                            </Button>
                          )}
                          {(img.status === 'needs_review' || img.status === 'low_confidence') && (
                            <>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 flex-1 text-green-600 hover:text-green-700"
                                title="Одобрить"
                                disabled={imageActionId === img.id}
                                onClick={() => handleImageAction(img.id, 'approve')}
                              >
                                <Check className="h-3.5 w-3.5" />
                              </Button>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 flex-1 text-destructive hover:text-destructive"
                                title="Отклонить"
                                disabled={imageActionId === img.id}
                                onClick={() => handleImageAction(img.id, 'reject')}
                              >
                                <X className="h-3.5 w-3.5" />
                              </Button>
                            </>
                          )}
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-7 w-7 flex-1 text-destructive hover:text-destructive"
                            title="Удалить"
                            disabled={imageActionId === img.id}
                            onClick={() => handleImageAction(img.id, 'delete')}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Действия */}
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Классификация товара</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="space-y-2">
                <p className="text-xs font-medium text-muted-foreground">Категория каталога</p>
                <CatalogCategoryPicker
                  categories={catalogCategories}
                  value={categoryAssignValue}
                  onValueChange={setCategoryAssignValue}
                  disabled={categoryAssignLoading}
                  placeholder="Выберите конечную подкатегорию"
                />
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    size="sm"
                    onClick={() => assignCatalogCategory(Number(categoryAssignValue))}
                    disabled={categoryAssignLoading || !catalogCategoryChanged}
                    title={!catalogCategoryChanged && categoryAssignValue
                      ? 'Эта категория уже применена'
                      : undefined}
                  >
                    {categoryAssignAction === 'apply' ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <Check className="mr-2 h-4 w-4" />
                    )}
                    Применить
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => assignCatalogCategory(null)}
                    disabled={categoryAssignLoading || !product.catalog_category}
                  >
                    {categoryAssignAction === 'remove' ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <X className="mr-2 h-4 w-4" />
                    )}
                    {categoryAssignAction === 'remove' ? 'Снимаем...' : 'Снять'}
                  </Button>
                </div>
                {!product.catalog_category && (
                  <p className="rounded-md border border-dashed p-2 text-xs text-muted-foreground">
                    Категория каталога не назначена. Автоматическая классификация ниже остаётся
                    подсказкой и не заменяет выбранную категорию.
                  </p>
                )}
              </div>
              <Separator />
              {product.catalog_classification ? (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={product.catalog_classification.review_status === 'rejected'
                        ? 'destructive'
                        : product.catalog_classification.needs_review ? 'outline' : 'secondary'}
                    >
                      {CATALOG_DOMAIN_LABELS[product.catalog_classification.domain]
                        ?? product.catalog_classification.domain}
                    </Badge>
                    <Badge variant="outline">
                      {classificationStatusLabel(product.catalog_classification)}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      Уверенность: {Math.round(product.catalog_classification.confidence * 100)}%
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {product.catalog_classification.reason}
                  </p>
                  {product.catalog_classification.needs_review
                    && product.catalog_classification.domain === 'unknown' && (
                    <p className="rounded-md border border-dashed p-2 text-xs text-muted-foreground">
                      Выберите категорию каталога, чтобы система поняла тип товара.
                    </p>
                  )}
                  {product.catalog_classification.needs_review
                    && product.catalog_classification.domain !== 'unknown' && (
                    <div className="grid grid-cols-2 gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => handleReview('classification', 'approve')}
                        disabled={reviewAction !== null}
                      >
                        {reviewAction === 'classification-main-approve' ? (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                          <Check className="mr-2 h-4 w-4" />
                        )}
                        Одобрить
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => handleReview('classification', 'reject')}
                        disabled={reviewAction !== null}
                      >
                        {reviewAction === 'classification-main-reject' ? (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                          <X className="mr-2 h-4 w-4" />
                        )}
                        Отклонить
                      </Button>
                    </div>
                  )}
                </>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Домен товара ещё не классифицирован.
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Работа с товаром</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="rounded-md border bg-muted/20 p-3">
                <p className="text-sm font-medium">Общие данные и медиа</p>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  Обогащайте товар, проверяйте найденные факты и фотографии здесь.
                  Эти данные можно использовать в нескольких маркетплейсах.
                </p>
              </div>
              <Button
                className="w-full"
                variant="outline"
                onClick={() => startEnrichment(true)}
                disabled={busy || searching || enriching || generatingDescription}
              >
                {actionLoading === 'enrich-generate' || enriching || generatingDescription ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="mr-2 h-4 w-4" />
                )}
                {enriching && parseThenGenerate
                  ? 'Обогащение перед генерацией...'
                  : generatingDescription
                    ? 'Генерация...'
                    : 'Обогатить и сгенерировать'}
              </Button>
              <Button
                className="w-full"
                variant="outline"
                onClick={startSearch}
                disabled={busy || searching || enriching || generatingDescription}
              >
                {searching || actionLoading === 'search' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Search className="mr-2 h-4 w-4" />
                )}
                {searching ? 'Поиск фото...' : 'Найти фото'}
              </Button>
              <Button
                className="w-full"
                variant="outline"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy || searching || enriching || generatingDescription}
              >
                {actionLoading === 'upload' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="mr-2 h-4 w-4" />
                )}
                Загрузить фото
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={handleUpload}
              />
              <div className="rounded-md border border-primary/20 bg-primary/5 p-3">
                <p className="text-sm font-medium">После проверки товара</p>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  Проверьте общие данные, медиа и карточку Ozon выше. В разделе
                  «Листинги» MAP ещё раз покажет ошибки отдельно для Avito и Ozon,
                  а вы выберете нужный кабинет и площадку для публикации.
                </p>
                <Button asChild className="mt-3 w-full">
                  <Link href={`/dashboard/listings?product=${product.id}`}>
                    <Store className="mr-2 h-4 w-4" />
                    Перейти в Листинги
                  </Link>
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Информация</CardTitle>
            </CardHeader>
            <CardContent className="divide-y text-sm">
              <Field label="ID" value={product.id} />
              <Field
                label="Создан"
                value={new Date(product.created_at).toLocaleDateString('ru-RU')}
              />
              <Field
                label="Обновлён"
                value={new Date(product.updated_at).toLocaleDateString('ru-RU')}
              />
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Предпросмотр фото */}
      {previewImg && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
          onClick={() => setPreviewImg(null)}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={previewImg}
            alt="Предпросмотр"
            className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain"
          />
        </div>
      )}
    </div>
  );
}
