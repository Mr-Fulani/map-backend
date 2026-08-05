'use client';

import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { productApi, imageApi } from '@/lib/api';
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
  Archive,
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
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import {
  CatalogCategoryPicker,
  type CatalogCategoryOption,
} from '@/components/products/catalog-category-picker';

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
  catalog_category: Pick<
    CatalogCategoryOption,
    'id' | 'name' | 'domain' | 'is_active'
  > | null;
  catalog_classification: ProductCatalogClassification | null;
}

interface ProductCatalogClassification {
  domain: string;
  confidence: number;
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

interface VehicleFitment {
  id: number;
  make: string;
  model: string;
  generation: string;
  modification: string;
  power_hp: number | null;
  needs_review: boolean;
  review_status: ReviewStatus;
}

interface ProductEnrichmentFact {
  id: number;
  source_id: string;
  fact_type: string;
  name: string;
  value: string;
  confidence: number;
  needs_review: boolean;
  review_status: ReviewStatus;
}

type ReviewStatus = 'pending' | 'approved' | 'rejected';

interface ProductParseJob {
  id: number;
  status: string;
  source_id: string;
  source_url: string;
  error_message: string;
  parsed_data: {
    image_urls?: string[];
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

const ENRICHMENT_STATUS_LABELS: Record<string, string> = {
  pending: 'Ожидает запуска',
  running: 'Идёт обогащение',
  success: 'Данные найдены',
  need_review: 'Данные найдены частично',
  not_found: 'Источник не нашёл товар',
  failed: 'Ошибка обогащения',
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

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-2">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-right text-sm font-medium">{value ?? '—'}</span>
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
  const [reviewAction, setReviewAction] = useState<string | null>(null);

  const [images, setImages] = useState<ProductImage[]>([]);
  const [imagesLoading, setImagesLoading] = useState(false);
  const [imageActionId, setImageActionId] = useState<number | null>(null);

  const [editingBrand, setEditingBrand] = useState(false);
  const [brandValue, setBrandValue] = useState('');
  const [savingBrand, setSavingBrand] = useState(false);

  const [searchTaskId, setSearchTaskId] = useState<string | null>(null);

  useEffect(() => {
    const previousHref = getPreviousDashboardHref();
    if (isProductsListHref(previousHref)) {
      setCatalogHref(previousHref);
      return;
    }

    if (isProductsListHref(returnTo)) {
      setCatalogHref(returnTo);
    }
  }, [returnTo]);
  const searching = searchTaskId !== null;
  const [generatingDescription, setGeneratingDescription] = useState(false);
  const [parseJobId, setParseJobId] = useState<number | null>(null);
  const [parseThenGenerate, setParseThenGenerate] = useState(false);
  const enriching = parseJobId !== null;
  const [webResearch, setWebResearch] = useState<WebResearchRun | null>(null);
  const [webResearchRunId, setWebResearchRunId] = useState<number | null>(null);
  const webResearchRunning = webResearchRunId !== null;

  const fileInputRef = useRef<HTMLInputElement>(null);
  const descriptionPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [previewImg, setPreviewImg] = useState<string | null>(null);

  const loadProduct = useCallback(async () => {
    const res = await productApi.get(Number(id));
    const nextProduct = res.data.data as ProductDetail;
    setProduct(nextProduct);
    setCategoryAssignValue(nextProduct.catalog_category?.id ? String(nextProduct.catalog_category.id) : '');
  }, [id]);

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
          toast.success('Описание сгенерировано на основе доступных данных');
        }
      } catch {
        if (descriptionPollRef.current) clearInterval(descriptionPollRef.current);
        descriptionPollRef.current = null;
        setGeneratingDescription(false);
        setParseThenGenerate(false);
      }
    }, 2000);
  }, [id]);

  useEffect(() => () => {
    if (descriptionPollRef.current) clearInterval(descriptionPollRef.current);
  }, []);

  async function saveBrand() {
    setSavingBrand(true);
    try {
      await productApi.updateBrand(Number(id), brandValue.trim());
      await loadProduct();
      setEditingBrand(false);
      toast.success('Бренд сохранён — импорт из источника его не перезапишет');
    } catch {
      toast.error('Не удалось сохранить бренд');
    } finally {
      setSavingBrand(false);
    }
  }

  useEffect(() => {
    loadProduct()
      .catch(() => toast.error('Товар не найден'))
      .finally(() => setLoading(false));
    loadWebResearch().catch(() => undefined);
  }, [loadProduct, loadWebResearch]);

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
  }, [webResearchRunId, loadProduct, product?.description_ai, waitForGeneratedDescription]);

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
    loadImages();
  }, [loadImages]);

  // Polling статуса поиска каждые 2с
  useEffect(() => {
    if (!searchTaskId) return;
    const interval = setInterval(async () => {
      try {
        const res = await imageApi.searchStatus(Number(id), searchTaskId);
        const { state, saved_count } = res.data.data;
        if (state !== 'running') {
          setSearchTaskId(null);
          if (state === 'done') {
            if (saved_count > 0) {
              toast.success(`Найдено фото: ${saved_count}`);
            } else {
              toast.warning('Фото не найдены — сервис поиска ограничил запросы. Попробуйте через минуту или загрузите вручную.');
            }
            loadImages();
          } else {
            toast.error('Поиск завершился с ошибкой');
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
    if (!parseJobId) return;
    const prevDescription = product?.description_ai ?? '';
    const deadline = Date.now() + 90_000;
    const interval = setInterval(async () => {
      if (Date.now() > deadline) {
        setParseJobId(null);
        setParseThenGenerate(false);
        clearInterval(interval);
        toast.warning('Обогащение заняло слишком долго. Проверьте очередь задач или попробуйте запустить снова.');
        return;
      }

      try {
        const res = await productApi.parseJobStatus(parseJobId);
        const job = res.data.data as ProductParseJob;
        if (!['pending', 'running'].includes(job.status)) {
          setParseJobId(null);
          await loadProduct();
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
          if (job.status === 'success') {
            toast.success('Данные товара обогащены');
          } else if (job.status === 'need_review') {
            toast.warning('Данные частично найдены. Проверьте блок «Обогащение данных» в карточке товара.');
          } else if (job.status === 'not_found') {
            toast.warning('Источник не нашёл этот товар');
          } else {
            toast.error(job.error_message || 'Обогащение завершилось с ошибкой');
          }
        }
      } catch {
        setParseJobId(null);
        setParseThenGenerate(false);
        toast.error('Ошибка при проверке статуса обогащения');
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [
    parseJobId,
    parseThenGenerate,
    product?.description_ai,
    loadProduct,
    loadWebResearch,
    waitForGeneratedDescription,
  ]);

  async function startEnrichment(generateAfter = false) {
    setActionLoading(generateAfter ? 'enrich-generate' : 'enrich');
    try {
      const res = await productApi.parse(Number(id), '', generateAfter);
      setParseJobId(res.data.data.job_id);
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

  async function runAction(action: 'publish' | 'archive') {
    setActionLoading(action);
    try {
      if (action === 'publish') {
        await productApi.publish(Number(id));
        toast.success('Листинги созданы. Откройте вкладку «Листинги» чтобы опубликовать.');
        return;
      }
      await productApi.archive(Number(id));
      toast.success('Объявления сняты с публикации и уходят в архив.');
    } catch (err: unknown) {
      const code = (err as { response?: { data?: { code?: string; message?: string } } })?.response?.data?.code;
      const message = (err as { response?: { data?: { message?: string } } })?.response?.data?.message;
      if (code === 'quota_exceeded') {
        toast.error('AI-кредиты исчерпаны. Обновите тариф в разделе Биллинг.');
      } else if (code === 'no_active_listings') {
        toast.warning(message ?? 'Нет активных объявлений для архивации.');
      } else if (code === 'no_accounts') {
        toast.error('Нет подключённых аккаунтов. Добавьте аккаунт в настройках.');
      } else {
        toast.error('Не удалось выполнить действие. Попробуйте ещё раз.');
      }
    } finally {
      setActionLoading(null);
    }
  }

  async function assignCatalogCategory(categoryId: number | null) {
    setCategoryAssignLoading(true);
    try {
      await productApi.assignCatalogCategory({
        product_ids: [Number(id)],
        catalog_category: categoryId,
      });
      await loadProduct();
      toast.success(categoryId ? 'Категория товара обновлена' : 'Категория товара снята');
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось изменить категорию товара');
    } finally {
      setCategoryAssignLoading(false);
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

  const busy = actionLoading !== null || webResearchRunning;

  return (
    <div className="space-y-6">
      {/* Навигация */}
      <div className="flex items-center gap-3">
        <Link href={catalogHref}>
          <Button variant="ghost" size="sm">
            <ArrowLeft className="mr-2 h-4 w-4" />
            Каталог
          </Button>
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="font-mono text-sm">{product.article}</span>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Основная информация + Фото */}
        <div className="space-y-6 lg:col-span-2">
          <Card>
            <CardHeader className="flex flex-row items-start justify-between">
              <div>
                <CardTitle className="text-xl">{product.name}</CardTitle>
                <p className="mt-1 font-mono text-sm text-muted-foreground">{product.article}</p>
              </div>
              <Badge variant={product.export_enabled ? 'default' : 'secondary'}>
                {product.export_enabled ? 'Выгружается' : 'Не выгружается'}
              </Badge>
            </CardHeader>
            <CardContent>
              <Separator className="mb-4" />
              <div className="divide-y">
                <div className="flex items-center justify-between gap-4 py-2">
                  <span className="text-sm text-muted-foreground">Бренд</span>
                  {editingBrand ? (
                    <div className="flex items-center gap-2">
                      <Input
                        className="h-8 w-48"
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
                    <span className="flex items-center gap-2 text-right text-sm font-medium">
                      {product.brand || '—'}
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

          {/* AI-описание */}
          {(product.description_ai || generatingDescription) && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium">AI-описание</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
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

          <Card className="overflow-hidden">
            <CardHeader className="border-b bg-muted/30">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <CardTitle className="text-sm font-medium">Обогащение данных</CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Достоверные факты из внешних каталогов для описания, OEM и применяемости.
                  </p>
                </div>
                <div className="flex flex-wrap gap-1">
                  {(product.parse_jobs_summary ?? (product.latest_parse_job ? [product.latest_parse_job] : [])).map((job) => {
                    const ok = job.status === 'success' || job.status === 'need_review';
                    return (
                      <Badge key={job.source_id} variant={ok ? 'secondary' : 'outline'} className="text-xs">
                        {job.source_id.charAt(0).toUpperCase() + job.source_id.slice(1)}:{' '}
                        {ENRICHMENT_STATUS_LABELS[job.status] ?? job.status}
                      </Badge>
                    );
                  })}
                  {webResearch && (
                    <Badge
                      variant={webResearch.status === 'need_review' ? 'secondary' : 'outline'}
                      className="text-xs"
                    >
                      Интернет: {WEB_RESEARCH_STATUS_LABELS[webResearch.status] ?? webResearch.status}
                    </Badge>
                  )}
                  {!product.latest_parse_job && (
                    <Badge variant="outline">Не запускалось</Badge>
                  )}
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-4 sm:pt-5">
              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                {product.latest_parse_job && (
                  <span>
                    Последний запуск: {new Date(product.latest_parse_job.created_at).toLocaleString('ru-RU')}
                  </span>
                )}
                {(product.parse_jobs_summary ?? []).find((j) => j.source_url) && (
                  <a
                    href={(product.parse_jobs_summary ?? []).find((j) => j.source_url)!.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-primary hover:underline"
                  >
                    Открыть источник
                  </a>
                )}
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
                  <p key={job.source_id} className="rounded-md bg-destructive/10 p-2 text-sm text-destructive">
                    <span className="font-medium capitalize">{job.source_id}:</span> {job.error_message}
                  </p>
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

                  {product.cross_codes.length > 0 && (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                        OEM / Cross-коды
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {product.cross_codes.slice(0, 12).map((cross) => (
                          <Badge key={cross.id} variant="outline" className="max-w-full whitespace-normal break-all px-2 py-1">
                            {cross.manufacturer ? `${cross.manufacturer}: ` : ''}
                            {cross.code}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  )}

                  {product.fitments.length > 0 && (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                        Применяемость
                      </p>
                      <div className="space-y-2">
                        {product.fitments.slice(0, 5).map((fitment) => (
                          <div key={fitment.id} className="flex flex-col gap-2 rounded-md border p-2 text-sm sm:flex-row sm:items-center sm:justify-between">
                            <div className="min-w-0">
                              <span className="break-words">
                                {[
                                  fitment.make,
                                  fitment.model,
                                  fitment.generation,
                                  fitment.modification,
                                  fitment.power_hp ? `${fitment.power_hp} л.с.` : '',
                                ].filter(Boolean).join(' ')}
                              </span>
                              <Badge
                                variant={fitment.review_status === 'rejected' ? 'destructive' : 'secondary'}
                                className="ml-2"
                              >
                                {reviewStatusLabel(fitment.review_status, fitment.needs_review)}
                              </Badge>
                            </div>
                            {fitment.needs_review && (
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
                        ))}
                      </div>
                    </div>
                  )}

                  {product.enrichment_facts.length > 0 && (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                        Факты для описания
                      </p>
                      <div className="space-y-2">
                        {product.enrichment_facts.slice(0, 5).map((fact) => (
                          <div key={fact.id} className="flex flex-col gap-2 rounded-md border p-2 text-sm sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0">
                              <p className="font-medium">{fact.name}</p>
                              <p className="break-words text-muted-foreground">{fact.value}</p>
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
                        ))}
                      </div>
                    </div>
                  )}

                </div>
              )}
            </CardContent>
          </Card>

          {/* Фотографии */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">
                Фотографии {images.length > 0 && `(${images.length})`}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {searching && (
                <div className="mb-4 flex items-start gap-2 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-200">
                  <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin" />
                  <div>
                    <p className="font-medium">Идёт поиск фотографий...</p>
                    <p className="mt-0.5 text-xs opacity-80">
                      Поиск может занять до 30 секунд — сервисы иногда ограничивают запросы, задача делает повторные попытки автоматически. Не нажимайте кнопку повторно.
                    </p>
                  </div>
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
                    disabled={categoryAssignLoading || !categoryAssignValue}
                  >
                    {categoryAssignLoading ? (
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
                    Снять
                  </Button>
                </div>
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
                      {reviewStatusLabel(
                        product.catalog_classification.review_status,
                        product.catalog_classification.needs_review,
                      )}
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
              <CardTitle className="text-sm font-medium">Действия</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <Button
                className="w-full"
                onClick={() => runAction('publish')}
                disabled={busy || searching || enriching || generatingDescription}
              >
                {actionLoading === 'publish' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="mr-2 h-4 w-4" />
                )}
                Опубликовать
              </Button>
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
              <Button
                className="w-full"
                variant="outline"
                onClick={() => runAction('archive')}
                disabled={busy || searching || enriching || generatingDescription}
              >
                {actionLoading === 'archive' ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Archive className="mr-2 h-4 w-4" />
                )}
                Архивировать
              </Button>
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
