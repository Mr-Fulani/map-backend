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
  isOzonBooleanAttribute,
  ozonAttributeValidationMessage,
  replaceOzonAttributeValue,
  type OzonDictionaryValue,
  type OzonOfferAttribute,
  type OzonOfferPreparation,
} from '@/lib/ozon-offer-preparation';

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

function rubles(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ₽`;
}

function percent(value: string): string {
  return `${Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}%`;
}

export interface OzonOfferPreparationCardHandle {
  saveAttributes: () => Promise<boolean>;
}

interface OzonOfferPreparationCardProps {
  productId: number;
  accounts: AccountOption[];
  onPreparationChange?: (preparation: OzonOfferPreparation | null) => void;
  showAccountSelector?: boolean;
  embedded?: boolean;
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
  refreshToken = 0,
}, ref) {
  const ozonAccounts = useMemo(
    () => accounts.filter((account) => account.marketplace === 'ozon' && account.is_active),
    [accounts],
  );
  const [accountChoiceId, setAccountChoiceId] = useState<number | null>(null);
  const [preparation, setPreparation] = useState<OzonOfferPreparation | null>(null);
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
  const accountId = ozonAccounts.some((account) => account.id === accountChoiceId)
    ? accountChoiceId
    : ozonAccounts.length === 1 ? ozonAccounts[0].id : null;
  const categoryTreeRequestRef = useRef(0);
  const loading = Boolean(accountId && loadedAccountId !== accountId);

  const applyPreparation = useCallback((next: OzonOfferPreparation | null) => {
    setPreparation(next);
    setAttributes(next?.attributes ?? []);
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
    try {
      const response = await productApi.getOzonOffer(productId, selectedAccountId);
      const next = envelopeData<OzonOfferPreparation>(response.data);
      applyPreparation(next);
    } catch {
      applyPreparation(null);
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
    setLoadedAccountId(null);
  }

  useEffect(() => {
    if (!accountId) return undefined;
    let active = true;
    productApi.getOzonOffer(productId, accountId)
      .then((response) => {
        if (!active) return;
        const next = envelopeData<OzonOfferPreparation>(response.data);
        applyPreparation(next);
      })
      .catch(() => {
        if (!active) return;
        applyPreparation(null);
        toast.error('Не удалось прочитать подготовку товара для Ozon.');
      })
      .finally(() => {
        if (active) setLoadedAccountId(accountId);
      });
    return () => { active = false; };
  }, [accountId, applyPreparation, productId, refreshToken]);

  useEffect(() => {
    if (!accountId) return;
    let active = true;
    categoryTreeRequestRef.current += 1;
    accountApi.getOzonCatalogTreeLevel(accountId, [])
      .then((response) => {
        if (!active) return;
        setCategoryTreeLevel(envelopeData<OzonCatalogTreeLevel>(response.data));
        setCategoryTreeLoading(false);
      })
      .catch(() => {
        if (!active) return;
        setCategoryTreeLevel(null);
        setCategoryTreeLoading(false);
        toast.error('Не удалось открыть локальное дерево категорий Ozon.');
      });
    return () => { active = false; };
  }, [accountId]);

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
    const invalid = ozonAttributesValidationErrors(attributes);
    if (invalid.length > 0) {
      toast.error(`${invalid[0].attribute.name}: ${invalid[0].message}`);
      return false;
    }
    const next = await updateOffer({
      attributes: ozonAttributesPayload(attributes),
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

  useImperativeHandle(ref, () => ({ saveAttributes }));

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

  return (
    <Card className={embedded ? 'border-0 bg-transparent shadow-none' : undefined}>
      <CardHeader className={`space-y-2 ${embedded ? 'px-0 pt-0' : ''}`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Подготовка карточки Ozon</CardTitle>
          {preparation && (
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
            : 'Это данные выбранного выше кабинета Ozon. Они не меняют категории, наценки или объявления Avito.'}
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
              <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-amber-950 dark:text-amber-100">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span><strong className="block">Нужно заполнить</strong>Без этого Ozon не примет карточку</span>
              </div>
              <div className="flex items-start gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-2.5 text-emerald-950 dark:text-emerald-100">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                <span><strong className="block">Готово</strong>Действие Тенанта не требуется</span>
              </div>
              <div className="rounded-md border border-dashed bg-muted/30 p-2.5 text-muted-foreground">
                <strong className="block text-foreground">Рекомендация</strong>
                Не блокирует подготовку карточки
              </div>
            </div>

            <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
              Служебный код карточки: <span className="font-mono">{preparation.draft.offer_id}</span>.
              Он создаётся один раз и не меняется при переименовании кабинета.
            </div>

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
                    Готово · MAP: {preparation.autofill.applied_count}
                  </Badge>
                  {preparation.autofill.preserved_count > 0 && (
                    <Badge
                      variant="outline"
                      className="border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200"
                    >
                      Готово · вручную: {preparation.autofill.preserved_count}
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

            <div className="space-y-3">
              <div>
                <p className="text-sm font-medium">Категория Ozon</p>
                <p className="text-xs text-muted-foreground">
                  Выбирается только конечный тип из отдельного локального дерева Ozon.
                </p>
              </div>
              {preparation.draft.category && (
                <div className="flex items-start gap-2 rounded-md border border-emerald-500/35 bg-emerald-500/5 p-3 text-sm">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                  <div>
                    <p className="text-muted-foreground">{preparation.draft.category.category_path}</p>
                    <p className="font-medium">{preparation.draft.category.type_name}</p>
                  </div>
                </div>
              )}
              <div className="space-y-3 rounded-md border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="flex items-center gap-2 text-sm font-medium">
                      <FolderTree className="h-4 w-4" /> Выбор по дереву Ozon
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Открывайте разделы по очереди и выберите конечный тип товара.
                    </p>
                  </div>
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
                    во вкладке «Настройки → Категории Ozon».
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

            {preparation.pricing && (
              <div className={`space-y-3 rounded-md border p-3 ${
                preparation.pricing.policy.effective_enabled
                  ? 'bg-muted/20'
                  : 'border-amber-500/30 bg-amber-500/5'
              }`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium">Цена для Ozon</p>
                    <p className="text-xs text-muted-foreground">
                      Рассчитывается отдельно для выбранного кабинета. Цена товара и Avito не меняются.
                    </p>
                  </div>
                  <Badge variant={preparation.pricing.policy.effective_enabled ? 'outline' : 'destructive'}>
                    {preparation.pricing.policy.effective_enabled
                      ? 'Категория включена'
                      : 'Категория выключена'}
                  </Badge>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div>
                    <p className="text-xs text-muted-foreground">Цена товара</p>
                    <p className="font-medium tabular-nums">{rubles(preparation.pricing.base_price)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Наценка Ozon</p>
                    <p className="font-medium tabular-nums">
                      {percent(preparation.pricing.effective_margin_pct)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Итоговая цена Ozon</p>
                    <p className="font-semibold tabular-nums">{rubles(preparation.pricing.final_price)}</p>
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">
                  {preparation.pricing.policy.margin_source
                    ? `Наценка задана для «${preparation.pricing.policy.margin_source.name}».`
                    : 'Используется стандартная наценка 0%. Задать её можно во вкладке «Настройки → Наценки Ozon».'}
                </p>
              </div>
            )}

            {preparation.draft.category && (
              <div className="space-y-3">
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
                  const selected = attribute.selected_values[0];
                  const autofill = preparation.autofill.fields[ozonAttributeIdentity(attribute)];
                  const recommendation = preparation.autofill.recommendations.find((item) => (
                    item.attribute_id === attribute.id
                    && (item.complex_id ?? 0) === attribute.complex_id
                  ));
                  const validationMessage = ozonAttributeValidationMessage(attribute);
                  const hasInvalidValue = Boolean(validationMessage);
                  const hasValidValue = Boolean(selected) && !hasInvalidValue;
                  const needsRequiredValue = attribute.is_required && !hasValidValue;
                  const booleanAttribute = isOzonBooleanAttribute(attribute);
                  return (
                    <div
                      key={key}
                      className={`space-y-2 rounded-md border border-l-4 p-3 ${
                        hasInvalidValue
                          ? 'border-red-500/50 bg-red-500/5'
                          : needsRequiredValue
                          ? 'border-amber-500/50 bg-amber-500/5'
                          : hasValidValue
                            ? 'border-emerald-500/35 bg-emerald-500/5'
                            : 'border-dashed bg-muted/20'
                      }`}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-sm font-medium">{attribute.name}</p>
                        {attribute.is_required && <Badge variant="outline">Обязательно</Badge>}
                        {hasInvalidValue ? (
                          <Badge
                            variant="outline"
                            className="border-red-500/50 bg-red-500/10 text-red-900 dark:text-red-100"
                          >
                            <AlertCircle className="mr-1 h-3 w-3" />
                            Исправить значение
                          </Badge>
                        ) : hasValidValue ? (
                          <Badge
                            variant="outline"
                            className="border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200"
                          >
                            <CheckCircle2 className="mr-1 h-3 w-3" />
                            {autofill?.state === 'auto_filled' ? 'Готово · MAP' : 'Готово · вручную'}
                          </Badge>
                        ) : needsRequiredValue ? (
                          <Badge
                            variant="outline"
                            className="border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100"
                          >
                            <AlertCircle className="mr-1 h-3 w-3" />
                            Заполнить вручную
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="border-dashed text-muted-foreground">
                            Можно пропустить
                          </Badge>
                        )}
                      </div>
                      {attribute.description && (
                        <p className="rounded bg-background/70 p-2 text-xs text-muted-foreground">
                          {attribute.description}
                        </p>
                      )}
                      {autofill && (
                        <p className="flex items-start gap-1.5 text-xs text-emerald-800 dark:text-emerald-200">
                          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span><strong>Почему заполнено:</strong> {autofill.source_label}. {autofill.message}</span>
                        </p>
                      )}
                      {recommendation && (
                        <div className="flex items-start gap-2 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-950 dark:text-amber-100">
                          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span>
                            <strong>Требуется ваше решение:</strong> {recommendation.message}
                          {recommendation.candidate && (
                            <span className="block pt-1 text-amber-800 dark:text-amber-200">
                              MAP нашёл вариант для сверки: {recommendation.candidate}
                            </span>
                          )}
                          </span>
                        </div>
                      )}
                      {validationMessage && (
                        <div className="flex items-start gap-2 rounded border border-red-500/40 bg-red-500/10 p-2 text-xs text-red-950 dark:text-red-100">
                          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span><strong>Сохранённое значение некорректно:</strong> {validationMessage}</span>
                        </div>
                      )}
                      {booleanAttribute ? (
                        <div className="space-y-2">
                          <div
                            role="group"
                            aria-label={attribute.name}
                            className="grid max-w-sm grid-cols-2 gap-2"
                          >
                            {([
                              ['false', 'Нет'],
                              ['true', 'Да'],
                            ] as const).map(([value, label]) => (
                              <Button
                                key={value}
                                type="button"
                                variant={selected?.value === value ? 'default' : 'outline'}
                                className="w-full"
                                onClick={() => setAttributes((current) => replaceOzonAttributeValue(
                                  current,
                                  attribute.id,
                                  attribute.complex_id,
                                  { value, dictionary_value_id: 0 },
                                ))}
                              >
                                {label}
                              </Button>
                            ))}
                          </div>
                          <p className="text-xs text-muted-foreground">
                            Выберите один вариант. MAP сам преобразует его в нужный формат Ozon.
                          </p>
                        </div>
                      ) : attribute.dictionary_id > 0 ? (
                        <>
                          {selected && (
                            <div className="flex items-center justify-between rounded border border-emerald-500/30 bg-emerald-500/10 p-2 text-sm">
                              <span className="flex items-center gap-1.5 font-medium">
                                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                                {selected.value}
                              </span>
                              <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                onClick={() => setAttributes((current) => replaceOzonAttributeValue(
                                  current,
                                  attribute.id,
                                  attribute.complex_id,
                                  null,
                                ))}
                              >
                                Очистить
                              </Button>
                            </div>
                          )}
                          <div className="flex flex-col gap-2 sm:flex-row">
                            <Input
                              aria-label={`Поиск: ${attribute.name}`}
                              className={needsRequiredValue ? 'border-amber-500/50 bg-background' : undefined}
                              value={dictionaryQueries[key] ?? ''}
                              onChange={(event) => setDictionaryQueries((current) => ({
                                ...current,
                                [key]: event.target.value,
                              }))}
                              placeholder="Введите название значения"
                            />
                            <Button
                              type="button"
                              variant="outline"
                              onClick={() => searchDictionary(attribute)}
                              disabled={action === `dictionary:${key}`}
                            >
                              {action === `dictionary:${key}` && (
                                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                              )}
                              Найти в Ozon
                            </Button>
                          </div>
                          {(dictionaryResults[key] ?? []).slice(0, 10).map((value) => (
                            <button
                              type="button"
                              key={value.id}
                              className="block w-full rounded border p-2 text-left text-sm hover:bg-muted/50"
                              onClick={() => setAttributes((current) => replaceOzonAttributeValue(
                                current,
                                attribute.id,
                                attribute.complex_id,
                                {
                                  value: value.value,
                                  dictionary_value_id: value.id,
                                },
                              ))}
                            >
                              {value.value}
                            </button>
                          ))}
                        </>
                      ) : (
                        <Input
                          aria-label={attribute.name}
                          className={needsRequiredValue ? 'border-amber-500/50 bg-background' : undefined}
                          value={selected?.value ?? ''}
                          onChange={(event) => setAttributes((current) => replaceOzonAttributeValue(
                            current,
                            attribute.id,
                            attribute.complex_id,
                            event.target.value.trim()
                              ? {
                                value: event.target.value,
                                dictionary_value_id: 0,
                              }
                              : null,
                          ))}
                          placeholder="Введите значение"
                        />
                      )}
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

            <div className="space-y-2">
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
          </>
        ) : null}
      </CardContent>
    </Card>
  );
});
