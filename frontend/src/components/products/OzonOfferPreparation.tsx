'use client';

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  FolderTree,
  Loader2,
  Search,
  ShieldCheck,
  WandSparkles,
} from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { OzonOfferAttributeEditor } from '@/components/products/OzonOfferAttributeEditor';
import { OzonOfferPricingEditor } from '@/components/products/OzonOfferPricingEditor';
import { accountApi, productApi } from '@/lib/api';
import type {
  OzonCatalogTreeLevel,
  OzonCatalogTreeOption,
  OzonCatalogTypeItem,
  OzonCatalogTypesPage,
} from '@/lib/marketplace-account-types';
import {
  ozonAttributesPayload,
  ozonAttributesValidationErrors,
  ozonAttributeIdentity,
  replaceOzonAttributeValue,
  type OzonDictionaryValue,
  type OzonOfferAttribute,
  type OzonOfferPreparation,
} from '@/lib/ozon-offer-preparation';
import {
  ozonMarginFromPrice,
  ozonPriceFromMargin,
  ozonPricingPayload,
  type OzonPricingMode,
} from '@/lib/ozon-offer-pricing';

interface AccountOption {
  id: number;
  name: string;
  marketplace: string;
  is_active: boolean;
}

function envelopeData<T>(body: unknown): T {
  return (body as { data: T }).data;
}

function treeOptionValue(option: OzonCatalogTreeOption): string {
  return [option.kind, option.description_category_id, option.type_id ?? 0].join(':');
}

export interface OzonOfferPreparationCardHandle {
  saveAttributes: () => Promise<boolean>;
  applyMarketPrice: (price: string) => boolean;
  focusField: (field: string) => boolean;
}

interface OzonOfferPreparationCardProps {
  productId: number;
  accounts: AccountOption[];
  onPreparationChange?: (preparation: OzonOfferPreparation | null) => void;
  showAccountSelector?: boolean;
  embedded?: boolean;
  showPricing?: boolean;
  showReadinessSummary?: boolean;
  title?: string;
  refreshToken?: string | number;
}

export const OzonOfferPreparationCard = forwardRef<
OzonOfferPreparationCardHandle,
OzonOfferPreparationCardProps
>(function OzonOfferPreparationCard({
  productId,
  accounts,
  onPreparationChange,
  showAccountSelector = true,
  embedded = false,
  showPricing = true,
  showReadinessSummary = true,
  title = 'Подготовка карточки Ozon',
  refreshToken = 0,
}, ref) {
  const ozonAccounts = useMemo(
    () => accounts.filter((account) => account.marketplace === 'ozon' && account.is_active),
    [accounts],
  );
  const [accountChoiceId, setAccountChoiceId] = useState<number | null>(null);
  const [preparation, setPreparation] = useState<OzonOfferPreparation | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [attributes, setAttributes] = useState<OzonOfferAttribute[]>([]);
  const [loadedAccountId, setLoadedAccountId] = useState<number | null>(null);
  const [action, setAction] = useState('');
  const [categoryTreeLoading, setCategoryTreeLoading] = useState(false);
  const [categoryTreeLevel, setCategoryTreeLevel] = useState<OzonCatalogTreeLevel | null>(null);
  const [categoryQuery, setCategoryQuery] = useState('');
  const [categoryResults, setCategoryResults] = useState<OzonCatalogTypeItem[]>([]);
  const [showOptional, setShowOptional] = useState(false);
  const [dictionaryQueries, setDictionaryQueries] = useState<Record<string, string>>({});
  const [dictionaryResults, setDictionaryResults] = useState<Record<string, OzonDictionaryValue[]>>({});
  const [marginOverride, setMarginOverride] = useState('');
  const [priceDraft, setPriceDraft] = useState('');
  const [pricingMode, setPricingMode] = useState<OzonPricingMode>('inherited');
  const accountId = ozonAccounts.some((account) => account.id === accountChoiceId)
    ? accountChoiceId
    : ozonAccounts.length === 1 ? ozonAccounts[0].id : null;
  const categoryTreeRequestRef = useRef(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const loading = Boolean(accountId && loadedAccountId !== accountId);

  const applyPreparation = useCallback((next: OzonOfferPreparation | null) => {
    setPreparation(next);
    setAttributes(next?.attributes ?? []);
    setMarginOverride(next?.pricing?.margin_override ?? '');
    setPriceDraft(next?.pricing?.final_price ?? '');
    setPricingMode(
      next?.pricing?.price_override !== null && next?.pricing?.price_override !== undefined
        ? 'price'
        : next?.pricing?.margin_override !== null && next?.pricing?.margin_override !== undefined
          ? 'margin'
          : 'inherited',
    );
    onPreparationChange?.(next);
  }, [onPreparationChange]);

  const loadCategoryTreeLevel = useCallback(async (
    selectedAccountId: number,
    parentIds: number[],
  ) => {
    const requestId = categoryTreeRequestRef.current + 1;
    categoryTreeRequestRef.current = requestId;
    setCategoryTreeLoading(true);
    try {
      const response = await accountApi.getOzonCatalogTreeLevel(selectedAccountId, parentIds);
      if (requestId !== categoryTreeRequestRef.current) return;
      setCategoryTreeLevel(envelopeData<OzonCatalogTreeLevel>(response.data));
    } catch {
      if (requestId !== categoryTreeRequestRef.current) return;
      setCategoryTreeLevel(null);
      toast.error('Не удалось открыть локальное дерево категорий Ozon.');
    } finally {
      if (requestId === categoryTreeRequestRef.current) setCategoryTreeLoading(false);
    }
  }, []);

  const loadPreparation = useCallback(async (selectedAccountId: number) => {
    setLoadError(false);
    setLoadedAccountId(null);
    try {
      const response = await productApi.getOzonOffer(productId, selectedAccountId);
      const next = envelopeData<OzonOfferPreparation>(response.data);
      applyPreparation(next);
    } catch {
      applyPreparation(null);
      setLoadError(true);
      toast.error('Не удалось прочитать подготовку товара для Ozon.');
    } finally {
      setLoadedAccountId(selectedAccountId);
    }
  }, [applyPreparation, productId]);

  function chooseAccount(nextAccountId: number) {
    categoryTreeRequestRef.current += 1;
    setAccountChoiceId(nextAccountId);
    setCategoryQuery('');
    setCategoryResults([]);
    setCategoryTreeLevel(null);
    setCategoryTreeLoading(false);
    setShowOptional(false);
    setDictionaryQueries({});
    setDictionaryResults({});
    setPreparation(null);
    setAttributes([]);
    setMarginOverride('');
    setPriceDraft('');
    setPricingMode('inherited');
    setLoadedAccountId(null);
  }

  useEffect(() => {
    if (!accountId) return undefined;
    let active = true;
    setLoadError(false);
    productApi.getOzonOffer(productId, accountId)
      .then((response) => {
        if (!active) return;
        const next = envelopeData<OzonOfferPreparation>(response.data);
        applyPreparation(next);
      })
      .catch(() => {
        if (!active) return;
        applyPreparation(null);
        setLoadError(true);
        toast.error('Не удалось прочитать подготовку товара для Ozon.');
      })
      .finally(() => {
        if (active) setLoadedAccountId(accountId);
      });
    return () => { active = false; };
  }, [accountId, applyPreparation, productId, refreshToken]);

  async function updateOffer(payload: Record<string, unknown>, actionName: string) {
    if (!accountId) return;
    setAction(actionName);
    try {
      const response = await productApi.updateOzonOffer(productId, {
        account_id: accountId,
        ...payload,
      });
      const next = envelopeData<OzonOfferPreparation>(response.data);
      applyPreparation(next);
      return next;
    } catch {
      toast.error('Не удалось сохранить подготовку Ozon. Проверьте выбранные данные.');
    } finally {
      setAction('');
    }
  }

  async function saveAttributes(): Promise<boolean> {
    let pricingPayload: {
      margin_pct?: string | null;
      price_override?: string | null;
    } = {};
    if (showPricing) {
      const pricing = ozonPricingPayload(pricingMode, marginOverride, priceDraft);
      if (!pricing.ok) {
        toast.error(pricing.message);
        return false;
      }
      pricingPayload = pricing.payload;
    }
    const invalid = ozonAttributesValidationErrors(attributes);
    if (invalid.length > 0) {
      toast.error(`${invalid[0].attribute.name}: ${invalid[0].message}`);
      return false;
    }
    const next = await updateOffer({
      attributes: ozonAttributesPayload(attributes),
      ...pricingPayload,
    }, 'attributes');
    if (!next) return false;
    if (next.preflight.errors.length > 0) {
      toast.warning(
        `Характеристики сохранены, но карточка пока не готова. Осталось исправить: ${next.preflight.errors.length}.`,
      );
    } else {
      toast.success('Характеристики сохранены и прошли проверку MAP.');
    }
    return true;
  }

  function applyMarketPrice(price: string): boolean {
    const margin = ozonMarginFromPrice(preparation?.pricing?.base_price ?? '', price);
    const selectedPrice = Number(price);
    if (margin === null || !Number.isFinite(selectedPrice)) {
      toast.error('Не удалось рассчитать наценку по выбранной цене.');
      return false;
    }
    setPriceDraft(selectedPrice.toFixed(2));
    setMarginOverride(margin);
    setPricingMode('price');
    return true;
  }

  function focusField(field: string): boolean {
    const exactField = field.startsWith('attribute:') ? field : null;
    const section = field === 'category'
      ? 'category'
      : field === 'price'
        ? 'pricing'
        : field === 'attributes' || exactField
          ? 'attributes'
          : null;
    if (!section) return false;
    const selector = exactField
      ? `[data-ozon-field="${exactField}"]`
      : `[data-ozon-section="${section}"]`;
    const target = rootRef.current?.querySelector<HTMLElement>(selector);
    if (!target) return false;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.querySelector<HTMLElement>('input, button, [role="combobox"]')?.focus({
      preventScroll: true,
    });
    return true;
  }

  useImperativeHandle(ref, () => ({ saveAttributes, applyMarketPrice, focusField }));

  async function savePricing() {
    const pricing = ozonPricingPayload(pricingMode, marginOverride, priceDraft);
    if (!pricing.ok) {
      toast.error(pricing.message);
      return;
    }
    const next = await updateOffer({ ...pricing.payload }, 'pricing');
    if (next) toast.success('Цена Ozon сохранена только для выбранного кабинета.');
  }

  function updateMargin(nextMargin: string) {
    setMarginOverride(nextMargin);
    const basePrice = Number(preparation?.pricing?.base_price);
    const normalized = nextMargin.trim();
    if (normalized === '') {
      setPricingMode('inherited');
      setPriceDraft(preparation?.pricing?.policy.effective_margin_pct
        ? (basePrice * (
          1 + Number(preparation.pricing.policy.effective_margin_pct) / 100
        )).toFixed(2)
        : preparation?.pricing?.final_price ?? '');
      return;
    }
    setPricingMode('margin');
    const nextPrice = ozonPriceFromMargin(String(basePrice), normalized);
    if (nextPrice !== null) setPriceDraft(nextPrice);
  }

  function updatePrice(nextPrice: string) {
    setPriceDraft(nextPrice);
    setPricingMode('price');
    const margin = ozonMarginFromPrice(preparation?.pricing?.base_price ?? '', nextPrice);
    if (margin !== null) setMarginOverride(margin);
  }

  async function autofillOffer(successMessage = 'Данные Ozon подготовлены для проверки.') {
    if (!accountId) return null;
    setAction('autofill');
    try {
      const response = await productApi.autofillOzonOffer(productId, accountId);
      const next = envelopeData<OzonOfferPreparation>(response.data);
      applyPreparation(next);
      setDictionaryQueries({});
      setDictionaryResults({});
      toast.success(successMessage);
      return next;
    } catch {
      toast.error('Не удалось подготовить поля Ozon. Сохранённые вручную данные не изменены.');
      return null;
    } finally {
      setAction('');
    }
  }

  async function findCategories() {
    if (!accountId) return;
    setAction('categories');
    try {
      const response = await accountApi.listOzonCatalogTypes(accountId, {
        search: categoryQuery.trim() || undefined,
        page: 1,
        page_size: 25,
        language: 'DEFAULT',
      });
      setCategoryResults((response.data as OzonCatalogTypesPage).data);
    } catch {
      toast.error('Не удалось найти категории в локальном справочнике Ozon.');
    } finally {
      setAction('');
    }
  }

  async function selectCategory(category: OzonCatalogTypeItem) {
    const next = await updateOffer({
      description_category_id: category.description_category_id,
      type_id: category.type_id,
    }, 'category');
    if (next) {
      setCategoryResults([]);
      setDictionaryQueries({});
      setDictionaryResults({});
      setShowOptional(false);
      await autofillOffer('Категория сохранена, безопасные поля заполнены.');
    }
  }

  async function chooseTreeOption(option: OzonCatalogTreeOption) {
    if (!accountId) return;
    if (option.kind === 'category') {
      const parentIds = [
        ...(categoryTreeLevel?.path.map((item) => item.description_category_id) ?? []),
        option.description_category_id,
      ];
      await loadCategoryTreeLevel(accountId, parentIds);
      return;
    }
    if (option.type_id === null) return;
    await selectCategory({
      description_category_id: option.description_category_id,
      type_id: option.type_id,
      category_path: option.category_path,
      type_name: option.name,
    });
  }

  async function openPreviousTreeLevel() {
    if (!accountId || !categoryTreeLevel) return;
    const parentIds = categoryTreeLevel.path
      .slice(0, -1)
      .map((item) => item.description_category_id);
    await loadCategoryTreeLevel(accountId, parentIds);
  }

  async function refreshAttributes() {
    if (!accountId || !preparation?.draft?.category) return;
    const confirmed = window.confirm(
      'MAP прочитает схему характеристик выбранной категории в Ozon. '
      + 'Товар, цена и остаток не изменятся. Продолжить?',
    );
    if (!confirmed) return;
    setAction('schema');
    try {
      const category = preparation.draft.category;
      await accountApi.refreshOzonCatalogAttributes(
        accountId,
        category.description_category_id,
        category.type_id,
      );
      await loadPreparation(accountId);
      setDictionaryQueries({});
      setDictionaryResults({});
      toast.success('Характеристики Ozon загружены в MAP.');
    } catch {
      toast.error('Не удалось загрузить характеристики Ozon.');
    } finally {
      setAction('');
    }
  }

  function attributeKey(attribute: OzonOfferAttribute) {
    const category = preparation?.draft?.category;
    return [
      accountId ?? 'none',
      category?.description_category_id ?? 'none',
      category?.type_id ?? 'none',
      attribute.complex_id,
      attribute.id,
    ].join(':');
  }

  async function searchDictionary(attribute: OzonOfferAttribute) {
    const category = preparation?.draft?.category;
    if (!accountId || !category) return;
    const key = attributeKey(attribute);
    const query = (dictionaryQueries[key] ?? '').trim();
    if (query.length < 2) {
      toast.warning('Введите минимум два символа.');
      return;
    }
    setAction(`dictionary:${key}`);
    try {
      const response = await accountApi.searchOzonAttributeValues(accountId, {
        description_category_id: category.description_category_id,
        type_id: category.type_id,
        attribute_id: attribute.id,
        query,
      });
      const data = envelopeData<{ values: OzonDictionaryValue[] }>(response.data);
      setDictionaryResults((current) => ({ ...current, [key]: data.values }));
    } catch {
      toast.error('Не удалось найти значение в справочнике Ozon.');
    } finally {
      setAction('');
    }
  }

  const visibleAttributes = attributes.filter(
    (attribute) => (
      showOptional
      || attribute.is_required
      || attribute.selected_values.length > 0
    ),
  );
  const categoryNeedsReview = preparation?.preflight.recommendations.some(
    (issue) => issue.field === 'category',
  ) ?? false;

  return (
    <div ref={rootRef}>
    <Card className={embedded ? 'border-0 bg-transparent shadow-none' : undefined}>
      <CardHeader className={`space-y-2 ${embedded ? 'px-0 pt-0' : ''}`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">{title}</CardTitle>
          {preparation && showReadinessSummary && (
            <Badge
              variant="outline"
              className={preparation.preflight.ready
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200'
                : 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'}
            >
              {preparation.preflight.ready
                ? 'Готово: обязательные поля заполнены'
                : `Нужно заполнить: ${preparation.preflight.errors.length}`}
            </Badge>
          )}
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {showAccountSelector
            ? 'Выберите конкретный кабинет Ozon. Категории и характеристики Ozon хранятся отдельно и не меняют категории, наценки или объявления Avito.'
            : 'Здесь заполняются только категория и характеристики Ozon. Жёлтые поля требуют действия, зелёные уже готовы. Данные Avito не изменяются.'}
        </p>
      </CardHeader>
      <CardContent className={`space-y-5 ${embedded ? 'px-0 pb-0' : ''}`}>
        {ozonAccounts.length === 0 ? (
          <p className="rounded-md border bg-muted/20 p-3 text-sm text-muted-foreground">
            Сначала подключите аккаунт Ozon в разделе «Настройки → Маркетплейсы».
          </p>
        ) : showAccountSelector ? (
          <div className="space-y-2">
            <label className="text-sm font-medium">Кабинет Ozon</label>
            <Select
              value={accountId ? String(accountId) : ''}
              onValueChange={(value) => chooseAccount(Number(value))}
            >
              <SelectTrigger aria-label="Кабинет Ozon"><SelectValue placeholder="Выберите кабинет" /></SelectTrigger>
              <SelectContent>
                {ozonAccounts.map((account) => (
                  <SelectItem key={account.id} value={String(account.id)}>{account.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Загружаем подготовку…
          </div>
        ) : loadError ? (
          <div className="space-y-3 rounded-md border border-destructive/30 bg-destructive/5 p-4">
            <p className="text-sm font-medium text-destructive">
              Не удалось загрузить карточку этого кабинета Ozon.
            </p>
            <p className="text-xs text-muted-foreground">
              Остальные кабинеты продолжают работать независимо.
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => accountId && loadPreparation(accountId)}
            >
              Повторить загрузку
            </Button>
          </div>
        ) : accountId && preparation && !preparation.draft ? (
          <div className="space-y-3 rounded-md border bg-muted/20 p-4">
            <div>
              <p className="text-sm font-medium">Подготовить карточку из данных товара</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                MAP перенесёт только подтверждённые значения. Категорию, ТН ВЭД,
                маркировку и неоднозначные справочники нужно будет проверить вручную.
              </p>
            </div>
            <Button onClick={() => autofillOffer()} disabled={Boolean(action)}>
              {action === 'autofill'
                ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                : <WandSparkles className="mr-2 h-4 w-4" />}
              Подготовить данные Ozon
            </Button>
          </div>
        ) : preparation?.draft ? (
          <>
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <div className="flex items-start gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-2.5 text-emerald-950 dark:text-emerald-100">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                <span><strong className="block">Зелёное · заполнил MAP</strong>Есть точный источник, действие не требуется</span>
              </div>
              <div className="flex items-start gap-2 rounded-md border border-blue-500/40 bg-blue-500/10 p-2.5 text-blue-950 dark:text-blue-100">
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
                <span><strong className="block">Синее · проверьте</strong>Значение найдено или введено вручную</span>
              </div>
              <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-amber-950 dark:text-amber-100">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span><strong className="block">Жёлтое · нужно решение</strong>Возьмите данные из указанного источника</span>
              </div>
            </div>

            <details className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
              <summary className="cursor-pointer font-medium text-foreground">
                Технические данные карточки
              </summary>
              <p className="mt-2">
                Служебный код: <span className="font-mono">{preparation.draft.offer_id}</span>.
                Он создаётся один раз и не меняется при переименовании кабинета.
              </p>
            </details>

            <div className="space-y-3 rounded-md border border-blue-500/20 bg-blue-500/5 p-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="flex items-center gap-2 text-sm font-medium">
                    <ShieldCheck className="h-4 w-4 text-blue-600" />
                    Автозаполнение MAP
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    Значения из товара и точные совпадения Ozon заполняются автоматически.
                    Рискованные поля MAP не придумывает.
                  </p>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => autofillOffer('Данные Ozon проверены и обновлены.')}
                  disabled={Boolean(action)}
                >
                  {action === 'autofill'
                    ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    : <WandSparkles className="mr-2 h-4 w-4" />}
                  Заполнить из товара
                </Button>
              </div>
              {preparation.autofill.status === 'not_started' ? (
                <p className="text-xs text-muted-foreground">
                  Автозаполнение ещё не запускалось для этого кабинета.
                </p>
              ) : (
                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge
                    variant="outline"
                    className="border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200"
                  >
                    MAP заполнил: {preparation.autofill.applied_count}
                  </Badge>
                  {preparation.autofill.preserved_count > 0 && (
                    <Badge
                      variant="outline"
                      className="border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200"
                    >
                      Сохранено вручную · проверить: {preparation.autofill.preserved_count}
                    </Badge>
                  )}
                  {preparation.autofill.recommendations.length > 0 && (
                    <Badge
                      variant="outline"
                      className="border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100"
                    >
                      Нужно решить: {preparation.autofill.recommendations.length}
                    </Badge>
                  )}
                </div>
              )}
              {preparation.autofill.recommendations
                .filter((item) => item.attribute_id === null)
                .map((item) => (
                  <div
                    key={item.code}
                    className="flex items-start gap-2 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-950 dark:text-amber-100"
                  >
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span><strong>Нужно ваше решение · {item.label}:</strong> {item.message}</span>
                  </div>
                ))}
            </div>

            {showPricing && preparation.pricing && (
              <div data-ozon-section="pricing">
                <OzonOfferPricingEditor
                  accountId={accountId}
                  pricing={preparation.pricing}
                  mode={pricingMode}
                  margin={marginOverride}
                  price={priceDraft}
                  saving={action === 'pricing'}
                  onMarginChange={updateMargin}
                  onPriceChange={updatePrice}
                  onSave={() => void savePricing()}
                />
              </div>
            )}

            <div className="space-y-3" data-ozon-section="category">
              <div>
                <p className="text-sm font-medium">Категория Ozon</p>
                <p className="text-xs text-muted-foreground">
                  Выбирается только конечный тип из отдельного локального дерева Ozon.
                </p>
              </div>
              {preparation.draft.category && (
                <div className={`flex items-start gap-2 rounded-md border p-3 text-sm ${
                  categoryNeedsReview
                    ? 'border-amber-500/40 bg-amber-500/10'
                    : 'border-emerald-500/35 bg-emerald-500/5'
                }`}
                >
                  {categoryNeedsReview
                    ? <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                    : <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />}
                  <div>
                    <p className="text-muted-foreground">{preparation.draft.category.category_path}</p>
                    <p className="font-medium">{preparation.draft.category.type_name}</p>
                    {categoryNeedsReview && (
                      <p className="mt-1 text-xs text-amber-950 dark:text-amber-100">
                        MAP нашёл возможное несоответствие названию товара — перепроверьте выбор.
                      </p>
                    )}
                  </div>
                </div>
              )}
              <div className={`space-y-3 rounded-md border border-l-4 p-3 ${
                preparation.draft.category
                  ? 'border-emerald-500/35 bg-emerald-500/5'
                  : 'border-amber-500/50 bg-amber-500/5'
              }`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="flex items-center gap-2 text-sm font-medium">
                      <FolderTree className="h-4 w-4" /> Выбор по дереву Ozon
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Открывайте разделы по очереди и выберите конечный тип товара.
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant="outline"
                      className={preparation.draft.category
                        ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200'
                        : 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'}
                    >
                      {preparation.draft.category ? 'Готово · выбрана' : 'Нужно выбрать'}
                    </Badge>
                    {(categoryTreeLevel?.path.length ?? 0) > 0 && (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={openPreviousTreeLevel}
                        disabled={categoryTreeLoading}
                      >
                        <ArrowLeft className="mr-1.5 h-4 w-4" /> Назад
                      </Button>
                    )}
                  </div>
                </div>
                {(categoryTreeLevel?.path.length ?? 0) > 0 && (
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    {categoryTreeLevel?.path.map((item) => item.name).join(' → ')}
                  </p>
                )}
                {categoryTreeLoading ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" /> Загружаем раздел…
                  </div>
                ) : categoryTreeLevel === null ? (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => accountId && loadCategoryTreeLevel(accountId, [])}
                  >
                    Открыть дерево категорий
                  </Button>
                ) : categoryTreeLevel.tree_revision === null ? (
                  <p className="text-sm text-muted-foreground">
                    Справочник Ozon ещё не загружен для этого кабинета. Обновите дерево
                    во вкладке «Настройки → Категории площадок → Ozon».
                  </p>
                ) : categoryTreeLevel.options.length > 0 ? (
                  <Select
                    value=""
                    disabled={Boolean(action)}
                    onValueChange={(value) => {
                      const option = categoryTreeLevel.options.find(
                        (item) => treeOptionValue(item) === value,
                      );
                      if (option) void chooseTreeOption(option);
                    }}
                  >
                    <SelectTrigger aria-label="Раздел или тип товара Ozon">
                      <SelectValue placeholder={
                        categoryTreeLevel.path.length
                          ? 'Выберите следующий раздел или тип товара'
                          : 'Выберите основной раздел'
                      } />
                    </SelectTrigger>
                    <SelectContent>
                      {categoryTreeLevel.options.map((option) => (
                        <SelectItem
                          key={treeOptionValue(option)}
                          value={treeOptionValue(option)}
                          disabled={option.kind === 'type' && !option.policy.effective_enabled}
                        >
                          {option.kind === 'category' ? `Раздел: ${option.name} ›` : option.name}
                          {!option.policy.effective_enabled && (
                            option.kind === 'type'
                              ? ' — выключено в настройках'
                              : ' — ветка выключена по умолчанию'
                          )}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    В этом разделе нет доступных типов. Вернитесь на уровень выше.
                  </p>
                )}
              </div>
              <details className="rounded-md border p-3">
                <summary className="cursor-pointer text-sm font-medium">
                  Быстрый поиск по названию — необязательно
                </summary>
                <div className="mt-3 space-y-2">
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Input
                      aria-label="Поиск категории для товара Ozon"
                      value={categoryQuery}
                      maxLength={120}
                      onChange={(event) => setCategoryQuery(event.target.value)}
                      placeholder="Например: тормозной шланг"
                    />
                    <Button type="button" variant="outline" onClick={findCategories} disabled={action === 'categories'}>
                      {action === 'categories' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
                      Найти
                    </Button>
                  </div>
                  {categoryResults.map((category) => (
                    <button
                      type="button"
                      key={`${category.description_category_id}:${category.type_id}`}
                      className="block w-full rounded-md border p-3 text-left text-sm hover:bg-muted/50"
                      onClick={() => selectCategory(category)}
                    >
                      <span className="block text-muted-foreground">{category.category_path}</span>
                      <span className="font-medium">{category.type_name}</span>
                    </button>
                  ))}
                </div>
              </details>
            </div>

            {preparation.draft.category && (
              <div className="space-y-3" data-ozon-section="attributes">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium">Характеристики Ozon</p>
                    <p className="text-xs text-muted-foreground">
                      Обязательные поля отмечены. Значения справочников выбираются по названию.
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={refreshAttributes}
                    disabled={action === 'schema'}
                  >
                    {action === 'schema' && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    {preparation.schema ? 'Обновить схему' : 'Загрузить характеристики'}
                  </Button>
                </div>

                {preparation.schema && visibleAttributes.map((attribute) => {
                  const key = attributeKey(attribute);
                  const autofill = preparation.autofill.fields[ozonAttributeIdentity(attribute)];
                  const autofillRecommendation = preparation.autofill.recommendations.find((item) => (
                    item.attribute_id === attribute.id
                    && (item.complex_id ?? 0) === attribute.complex_id
                  ));
                  const qualityRecommendation = preparation.preflight.recommendations.find((item) => (
                    item.field === `attribute:${attribute.complex_id}:${attribute.id}`
                  ));
                  const recommendation = autofillRecommendation ?? (
                    qualityRecommendation
                      ? {
                          code: qualityRecommendation.code,
                          attribute_id: attribute.id,
                          complex_id: attribute.complex_id,
                          label: qualityRecommendation.label,
                          message: qualityRecommendation.message,
                          candidate: '',
                        }
                      : undefined
                  );
                  return (
                    <div
                      key={key}
                      data-ozon-field={`attribute:${attribute.complex_id}:${attribute.id}`}
                    >
                    <OzonOfferAttributeEditor
                      attribute={attribute}
                      autofill={autofill}
                      recommendation={recommendation}
                      dictionaryQuery={dictionaryQueries[key] ?? ''}
                      dictionaryResults={dictionaryResults[key] ?? []}
                      dictionaryLoading={action === `dictionary:${key}`}
                      onDictionaryQueryChange={(value) => setDictionaryQueries((current) => ({
                        ...current,
                        [key]: value,
                      }))}
                      onDictionarySearch={() => void searchDictionary(attribute)}
                      onValueChange={(value) => setAttributes((current) => replaceOzonAttributeValue(
                        current,
                        attribute.id,
                        attribute.complex_id,
                        value,
                      ))}
                    />
                    </div>
                  );
                })}

                {preparation.schema && (
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      onClick={saveAttributes}
                      disabled={action === 'attributes'}
                    >
                      {action === 'attributes' && (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      )}
                      Сохранить характеристики
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => setShowOptional((value) => !value)}
                    >
                      {showOptional ? 'Скрыть необязательные' : 'Показать необязательные'}
                    </Button>
                  </div>
                )}
              </div>
            )}

            {showReadinessSummary && (
            <div className="space-y-2" data-ozon-section="readiness">
              <p className="text-sm font-medium">Проверка готовности</p>
              {preparation.preflight.ready ? (
                <div className="flex items-start gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-950 dark:text-emerald-100">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                  <span><strong>Готово.</strong> Все обязательные данные заполнены. Отправки в Ozon ещё нет.</span>
                </div>
              ) : preparation.preflight.errors.map((issue) => (
                <div key={`${issue.code}:${issue.field}`} className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-950 dark:text-amber-100">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span><strong>Блокирует отправку · {issue.label}:</strong> {issue.message}</span>
                </div>
              ))}
              {preparation.preflight.recommendations.map((issue) => (
                <div key={`${issue.code}:${issue.field}`} className="flex items-start gap-2 rounded-md border border-blue-500/30 bg-blue-500/5 p-3 text-xs text-blue-950 dark:text-blue-100">
                  <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
                  <span><strong>Рекомендация · не блокирует · {issue.label}:</strong> {issue.message}</span>
                </div>
              ))}
            </div>
            )}
          </>
        ) : null}
      </CardContent>
    </Card>
    </div>
  );
});
