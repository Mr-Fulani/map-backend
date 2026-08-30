'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle2, Loader2, Search } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { accountApi, productApi } from '@/lib/api';
import type { OzonCatalogTypeItem, OzonCatalogTypesPage } from '@/lib/marketplace-account-types';
import {
  ozonAttributesPayload,
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

export function OzonOfferPreparationCard({
  productId,
  accounts,
}: {
  productId: number;
  accounts: AccountOption[];
}) {
  const ozonAccounts = useMemo(
    () => accounts.filter((account) => account.marketplace === 'ozon' && account.is_active),
    [accounts],
  );
  const [accountChoiceId, setAccountChoiceId] = useState<number | null>(null);
  const [preparation, setPreparation] = useState<OzonOfferPreparation | null>(null);
  const [attributes, setAttributes] = useState<OzonOfferAttribute[]>([]);
  const [loadedAccountId, setLoadedAccountId] = useState<number | null>(null);
  const [action, setAction] = useState('');
  const [categoryQuery, setCategoryQuery] = useState('');
  const [categoryResults, setCategoryResults] = useState<OzonCatalogTypeItem[]>([]);
  const [showOptional, setShowOptional] = useState(false);
  const [dictionaryQueries, setDictionaryQueries] = useState<Record<string, string>>({});
  const [dictionaryResults, setDictionaryResults] = useState<Record<string, OzonDictionaryValue[]>>({});
  const accountId = ozonAccounts.some((account) => account.id === accountChoiceId)
    ? accountChoiceId
    : ozonAccounts.length === 1 ? ozonAccounts[0].id : null;
  const loading = Boolean(accountId && loadedAccountId !== accountId);

  const loadPreparation = useCallback(async (selectedAccountId: number) => {
    try {
      const response = await productApi.getOzonOffer(productId, selectedAccountId);
      const next = envelopeData<OzonOfferPreparation>(response.data);
      setPreparation(next);
      setAttributes(next.attributes);
    } catch {
      setPreparation(null);
      setAttributes([]);
      toast.error('Не удалось прочитать подготовку товара для Ozon.');
    } finally {
      setLoadedAccountId(selectedAccountId);
    }
  }, [productId]);

  function chooseAccount(nextAccountId: number) {
    setAccountChoiceId(nextAccountId);
    setCategoryQuery('');
    setCategoryResults([]);
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
        setPreparation(next);
        setAttributes(next.attributes);
      })
      .catch(() => {
        if (!active) return;
        setPreparation(null);
        setAttributes([]);
        toast.error('Не удалось прочитать подготовку товара для Ozon.');
      })
      .finally(() => {
        if (active) setLoadedAccountId(accountId);
      });
    return () => { active = false; };
  }, [accountId, productId]);

  async function updateOffer(payload: Record<string, unknown>, actionName: string) {
    if (!accountId) return;
    setAction(actionName);
    try {
      const response = await productApi.updateOzonOffer(productId, {
        account_id: accountId,
        ...payload,
      });
      const next = envelopeData<OzonOfferPreparation>(response.data);
      setPreparation(next);
      setAttributes(next.attributes);
      return next;
    } catch {
      toast.error('Не удалось сохранить подготовку Ozon. Проверьте выбранные данные.');
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
      toast.success('Категория Ozon сохранена.');
    }
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
    <Card>
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Подготовка карточки Ozon</CardTitle>
          {preparation && (
            <Badge variant={preparation.preflight.ready ? 'default' : 'outline'}>
              {preparation.preflight.ready
                ? 'Готово к будущей отправке'
                : `Нужно исправить: ${preparation.preflight.errors.length}`}
            </Badge>
          )}
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Выберите конкретный кабинет Ozon. Категории и характеристики Ozon хранятся
          отдельно и не меняют категории, наценки или объявления Avito.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {ozonAccounts.length === 0 ? (
          <p className="rounded-md border bg-muted/20 p-3 text-sm text-muted-foreground">
            Сначала подключите аккаунт Ozon в разделе «Настройки → Маркетплейсы».
          </p>
        ) : (
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
        )}

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Загружаем подготовку…
          </div>
        ) : accountId && preparation && !preparation.draft ? (
          <Button onClick={() => updateOffer({}, 'start')} disabled={Boolean(action)}>
            {action === 'start' && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Начать подготовку для этого кабинета
          </Button>
        ) : preparation?.draft ? (
          <>
            <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
              Служебный код карточки: <span className="font-mono">{preparation.draft.offer_id}</span>.
              Он создаётся один раз и не меняется при переименовании кабинета.
            </div>

            <div className="space-y-3">
              <div>
                <p className="text-sm font-medium">Категория Ozon</p>
                <p className="text-xs text-muted-foreground">
                  Выбирается только конечный тип из отдельного локального дерева Ozon.
                </p>
              </div>
              {preparation.draft.category && (
                <div className="rounded-md border p-3 text-sm">
                  <p>{preparation.draft.category.category_path}</p>
                  <p className="font-medium">{preparation.draft.category.type_name}</p>
                </div>
              )}
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  aria-label="Поиск категории для товара Ozon"
                  value={categoryQuery}
                  maxLength={120}
                  onChange={(event) => setCategoryQuery(event.target.value)}
                  placeholder="Например: тормозные колодки"
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
                  return (
                    <div key={key} className="space-y-2 rounded-md border p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-sm font-medium">{attribute.name}</p>
                        {attribute.is_required && <Badge variant="outline">Обязательно</Badge>}
                      </div>
                      {attribute.description && (
                        <p className="text-xs text-muted-foreground">{attribute.description}</p>
                      )}
                      {attribute.dictionary_id > 0 ? (
                        <>
                          {selected && (
                            <div className="flex items-center justify-between rounded bg-muted p-2 text-sm">
                              <span>{selected.value}</span>
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
                      onClick={() => updateOffer({
                        attributes: ozonAttributesPayload(attributes),
                      }, 'attributes')}
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
                <div className="flex items-start gap-2 rounded-md border border-green-500/30 bg-green-500/5 p-3 text-sm">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 text-green-600" />
                  Все обязательные данные заполнены. Отправки в Ozon ещё нет.
                </div>
              ) : preparation.preflight.errors.map((issue) => (
                <div key={`${issue.code}:${issue.field}`} className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-sm">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                  <span><strong>{issue.label}:</strong> {issue.message}</span>
                </div>
              ))}
              {preparation.preflight.recommendations.map((issue) => (
                <p key={`${issue.code}:${issue.field}`} className="text-xs text-muted-foreground">
                  Рекомендация — {issue.label.toLowerCase()}: {issue.message}
                </p>
              ))}
            </div>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
