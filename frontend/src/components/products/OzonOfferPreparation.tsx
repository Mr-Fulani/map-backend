'use client';

import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle2, Loader2, Search } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { accountApi, productApi } from '@/lib/api';
import type { OzonCatalogTypeItem, OzonCatalogTypesPage } from '@/lib/marketplace-account-types';
import type { OzonOfferPreparation } from '@/lib/ozon-offer-preparation';

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
  const [loadedAccountId, setLoadedAccountId] = useState<number | null>(null);
  const [preparation, setPreparation] = useState<OzonOfferPreparation | null>(null);
  const [action, setAction] = useState('');
  const [categoryQuery, setCategoryQuery] = useState('');
  const [categoryResults, setCategoryResults] = useState<OzonCatalogTypeItem[]>([]);
  const accountId = ozonAccounts.some((account) => account.id === accountChoiceId)
    ? accountChoiceId
    : ozonAccounts.length === 1 ? ozonAccounts[0].id : null;
  const loading = Boolean(accountId && loadedAccountId !== accountId);

  useEffect(() => {
    if (!accountId) return undefined;
    let active = true;
    productApi.getOzonOffer(productId, accountId)
      .then((response) => {
        if (active) setPreparation(envelopeData<OzonOfferPreparation>(response.data));
      })
      .catch(() => {
        if (!active) return;
        setPreparation(null);
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
      setPreparation(envelopeData<OzonOfferPreparation>(response.data));
      return true;
    } catch {
      toast.error('Не удалось сохранить подготовку Ozon. Проверьте выбранные данные.');
      return false;
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
    const saved = await updateOffer({
      description_category_id: category.description_category_id,
      type_id: category.type_id,
    }, 'category');
    if (saved) {
      setCategoryResults([]);
      toast.success('Категория Ozon сохранена.');
    }
  }

  return (
    <Card>
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Подготовка карточки Ozon</CardTitle>
          {preparation && (
            <Badge variant={preparation.preflight.ready ? 'default' : 'outline'}>
              {preparation.preflight.ready
                ? 'Базовые данные готовы'
                : `Нужно исправить: ${preparation.preflight.errors.length}`}
            </Badge>
          )}
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Выберите конкретный кабинет Ozon. Категория хранится отдельно и не меняет
          категории, наценки или объявления Avito.
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
              onValueChange={(value) => setAccountChoiceId(Number(value))}
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

            <div className="space-y-2">
              <p className="text-sm font-medium">Базовая проверка готовности</p>
              {preparation.preflight.ready ? (
                <div className="flex items-start gap-2 rounded-md border border-green-500/30 bg-green-500/5 p-3 text-sm">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 text-green-600" />
                  Аккаунт, склад, категория и основные данные готовы. Отправки в Ozon ещё нет.
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
