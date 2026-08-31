'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Folder,
  Loader2,
  RotateCcw,
  Search,
  Tag,
} from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { accountApi } from '@/lib/api';
import type {
  OzonCatalogTreeLevel,
  OzonCatalogTreeOption,
} from '@/lib/marketplace-account-types';
import {
  normalizeOzonMargin,
  ozonCategoryPathIds,
  ozonEnabledOverride,
  ozonPolicyDraft,
  ozonPolicyKey,
  ozonPolicySourceLabel,
  ozonTreeLevelResponse,
  type OzonCategoryPolicyDraft,
} from '@/lib/ozon-category-policy-ui';

interface OzonCategoryPolicyTreeProps {
  accountId: number;
  accountName: string;
  canManage: boolean;
  mode: 'categories' | 'margins';
}

function policyErrorDetails(error: unknown): { code: string; message: string } {
  const response = (error as {
    response?: { status?: number; data?: { code?: unknown; message?: unknown } };
  } | null)?.response;
  const code = typeof response?.data?.code === 'string' ? response.data.code : '';
  if (code === 'tree_revision_outdated') {
    return {
      code,
      message: 'Дерево Ozon обновилось. MAP загрузил актуальную версию — повторите настройку.',
    };
  }
  if (code === 'tree_required') {
    return { code, message: 'Сначала загрузите справочник категорий этого кабинета Ozon.' };
  }
  if (response?.status === 403) {
    return { code, message: 'Менять категории и наценки может владелец или администратор тенанта.' };
  }
  if (code === 'invalid_category_path' || code === 'invalid_category_type') {
    return { code, message: 'Выбранная категория отсутствует в актуальном дереве Ozon.' };
  }
  if (code === 'inactive_category') {
    return { code, message: 'В этой категории Ozon больше нет доступных типов товаров.' };
  }
  return { code, message: 'Не удалось сохранить локальные настройки категории Ozon.' };
}

function draftsFromLevel(
  level: OzonCatalogTreeLevel,
): Record<string, OzonCategoryPolicyDraft> {
  return Object.fromEntries(
    level.options.map((option) => [ozonPolicyKey(option), ozonPolicyDraft(option)]),
  );
}

export function OzonCategoryPolicyTree({
  accountId,
  accountName,
  canManage,
  mode,
}: OzonCategoryPolicyTreeProps) {
  const [level, setLevel] = useState<OzonCatalogTreeLevel | null>(null);
  const [parentIds, setParentIds] = useState<number[]>([]);
  const [drafts, setDrafts] = useState<Record<string, OzonCategoryPolicyDraft>>({});
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const requestId = useRef(0);

  const loadLevel = useCallback(async (nextParentIds: number[]) => {
    const currentRequest = requestId.current + 1;
    requestId.current = currentRequest;
    setLoading(true);
    setLoadError(null);
    try {
      const response = await accountApi.getOzonCatalogTreeLevel(accountId, nextParentIds);
      const nextLevel = ozonTreeLevelResponse(response.data);
      if (requestId.current !== currentRequest) return;
      setLevel(nextLevel);
      setParentIds(nextParentIds);
      setDrafts(draftsFromLevel(nextLevel));
      setValidationErrors({});
      setSearch('');
    } catch {
      if (requestId.current === currentRequest) {
        setLoadError('Не удалось прочитать локальное дерево. Запрос в кабинет Ozon не выполнялся.');
      }
    } finally {
      if (requestId.current === currentRequest) setLoading(false);
    }
  }, [accountId]);

  useEffect(() => {
    let active = true;
    const currentRequest = requestId.current + 1;
    requestId.current = currentRequest;
    accountApi.getOzonCatalogTreeLevel(accountId, [])
      .then((response) => {
        if (!active || requestId.current !== currentRequest) return;
        const nextLevel = ozonTreeLevelResponse(response.data);
        setLevel(nextLevel);
        setParentIds([]);
        setDrafts(draftsFromLevel(nextLevel));
        setValidationErrors({});
        setLoadError(null);
      })
      .catch(() => {
        if (active && requestId.current === currentRequest) {
          setLoadError('Не удалось прочитать локальное дерево. Запрос в кабинет Ozon не выполнялся.');
        }
      })
      .finally(() => {
        if (active && requestId.current === currentRequest) setLoading(false);
      });
    return () => {
      active = false;
      requestId.current += 1;
    };
  }, [accountId]);

  const visibleOptions = useMemo(() => {
    if (!level) return [];
    const query = search.trim().toLocaleLowerCase('ru-RU');
    if (!query) return level.options;
    return level.options.filter((option) => (
      option.name.toLocaleLowerCase('ru-RU').includes(query)
      || option.category_path.toLocaleLowerCase('ru-RU').includes(query)
    ));
  }, [level, search]);

  function updateDraft(
    option: OzonCatalogTreeOption,
    change: Partial<OzonCategoryPolicyDraft>,
  ) {
    const key = ozonPolicyKey(option);
    setDrafts((current) => ({
      ...current,
      [key]: { ...(current[key] ?? ozonPolicyDraft(option)), ...change },
    }));
    setValidationErrors((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  async function savePolicy(
    option: OzonCatalogTreeOption,
    override?: OzonCategoryPolicyDraft,
  ) {
    if (!canManage || !level?.tree_revision) return;
    const key = ozonPolicyKey(option);
    const draft = override ?? drafts[key] ?? ozonPolicyDraft(option);
    const margin = normalizeOzonMargin(draft.margin);
    if (margin.error) {
      setValidationErrors((current) => ({ ...current, [key]: margin.error as string }));
      return;
    }

    setSavingKey(key);
    try {
      await accountApi.updateOzonCategoryPolicy(accountId, {
        description_category_id: option.description_category_id,
        type_id: option.type_id,
        category_path_ids: ozonCategoryPathIds(level, option),
        tree_revision: level.tree_revision,
        enabled_override: ozonEnabledOverride(draft.enabled),
        margin_pct: margin.value,
      });
      await loadLevel(parentIds);
      toast.success(`Настройки «${option.name}» сохранены для кабинета «${accountName}».`);
    } catch (error: unknown) {
      const details = policyErrorDetails(error);
      toast.error(details.message);
      if (details.code === 'tree_revision_outdated') await loadLevel([]);
    } finally {
      setSavingKey(null);
    }
  }

  function resetPolicy(option: OzonCatalogTreeOption) {
    const current = drafts[ozonPolicyKey(option)] ?? ozonPolicyDraft(option);
    const inherited: OzonCategoryPolicyDraft = mode === 'categories'
      ? { ...current, enabled: 'inherit' }
      : { ...current, margin: '' };
    updateDraft(option, inherited);
    void savePolicy(option, inherited);
  }

  function toggleCategory(option: OzonCatalogTreeOption) {
    const current = drafts[ozonPolicyKey(option)] ?? ozonPolicyDraft(option);
    const next: OzonCategoryPolicyDraft = {
      ...current,
      enabled: option.policy.effective_enabled ? 'disabled' : 'enabled',
    };
    updateDraft(option, next);
    void savePolicy(option, next);
  }

  if (loading && !level) {
    return <div className="h-28 animate-pulse rounded-md bg-muted" />;
  }

  if (loadError && !level) {
    return (
      <div className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-muted-foreground">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
        <div>
          <p>{loadError}</p>
          <Button className="mt-2" size="sm" variant="outline" onClick={() => void loadLevel([])}>
            Повторить
          </Button>
        </div>
      </div>
    );
  }

  if (!level?.tree_revision) {
    return (
      <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        Справочник этого кабинета пока не загружен. Закройте настройки и нажмите
        «Загрузить категории».
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          {mode === 'categories' ? 'Категории' : 'Наценки'} кабинета «{accountName}»
        </p>
        <p className="mt-1">
          {mode === 'categories'
            ? 'Включайте только нужные разделы Ozon. Настройка раздела наследуется всеми вложенными категориями и типами товара.'
            : 'Пустое значение наследует наценку ближайшего родительского раздела. Цена товара и наценки Avito не изменяются.'}
        </p>
      </div>

      <nav aria-label="Путь категории Ozon" className="flex flex-wrap items-center gap-1 text-xs">
        <Button
          type="button"
          size="sm"
          variant={level.path.length === 0 ? 'secondary' : 'ghost'}
          disabled={loading}
          onClick={() => void loadLevel([])}
        >
          Все категории
        </Button>
        {level.path.map((item, index) => (
          <div key={item.description_category_id} className="flex items-center gap-1">
            <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
            <Button
              type="button"
              size="sm"
              variant={index === level.path.length - 1 ? 'secondary' : 'ghost'}
              disabled={loading}
              onClick={() => void loadLevel(
                level.path.slice(0, index + 1).map((pathItem) => pathItem.description_category_id),
              )}
            >
              {item.name}
            </Button>
          </div>
        ))}
      </nav>

      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Фильтр текущего раздела Ozon"
          className="pl-9"
          value={search}
          maxLength={120}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Фильтр в текущем разделе"
        />
      </div>

      {loadError && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-muted-foreground">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          {loadError}
        </div>
      )}

      {loading ? (
        <div className="h-28 animate-pulse rounded-md bg-muted" />
      ) : visibleOptions.length === 0 ? (
        <p className="rounded-md border border-dashed p-4 text-center text-sm text-muted-foreground">
          {search.trim() ? 'В этом разделе ничего не найдено.' : 'В разделе нет доступных категорий.'}
        </p>
      ) : (
        <div className="space-y-2">
          {visibleOptions.map((option) => {
            const key = ozonPolicyKey(option);
            const draft = drafts[key] ?? ozonPolicyDraft(option);
            const isSaving = savingKey === key;
            const hasOwnSetting = mode === 'categories'
              ? option.policy.enabled_override !== null
              : option.policy.margin_pct !== null;
            return (
              <div
                key={key}
                className="flex flex-col gap-3 rounded-lg border bg-background p-3 md:flex-row md:items-center md:justify-between"
              >
                <div className="flex min-w-0 items-center gap-3">
                  <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-md border bg-muted/60">
                    {option.kind === 'category'
                      ? <Folder className="h-5 w-5 text-blue-500" />
                      : <Tag className="h-5 w-5 text-violet-500" />}
                  </div>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="break-words font-medium">{option.name}</p>
                      <Badge variant="outline">Официальная Ozon</Badge>
                    </div>
                    <p className="mt-0.5 break-words text-xs text-muted-foreground">
                      {option.category_path}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {mode === 'categories'
                        ? `Статус: ${ozonPolicySourceLabel(
                          option.policy.enabled_source,
                          option,
                          'включено по умолчанию',
                        )}`
                        : `Итог: ${option.policy.effective_margin_pct}% · ${ozonPolicySourceLabel(
                          option.policy.margin_source,
                          option,
                          'без наценки',
                        )}`}
                    </p>
                  </div>
                </div>

                {mode === 'categories' ? (
                  <div className="flex flex-wrap items-center gap-2 md:justify-end">
                    {option.kind === 'category' && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={loading || savingKey !== null}
                        onClick={() => void loadLevel(ozonCategoryPathIds(level, option))}
                      >
                        Открыть ветку
                        <ChevronRight className="ml-1 h-3.5 w-3.5" />
                      </Button>
                    )}
                    {hasOwnSetting && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={!canManage || savingKey !== null}
                        onClick={() => resetPolicy(option)}
                      >
                        <RotateCcw className="mr-1 h-3.5 w-3.5" />
                        Наследовать
                      </Button>
                    )}
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!canManage || savingKey !== null}
                      onClick={() => toggleCategory(option)}
                    >
                      {isSaving && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
                      {option.policy.effective_enabled ? 'Отключить' : 'Включить'}
                    </Button>
                    <Badge variant={option.policy.effective_enabled ? 'default' : 'secondary'}>
                      {option.policy.effective_enabled ? 'Активна' : 'Отключена'}
                    </Badge>
                  </div>
                ) : (
                  <div className="flex w-full flex-col gap-2 md:w-auto md:min-w-[520px] md:flex-row md:items-center md:justify-end">
                    {option.kind === 'category' && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={loading || savingKey !== null}
                        onClick={() => void loadLevel(ozonCategoryPathIds(level, option))}
                      >
                        Внутрь
                        <ChevronRight className="ml-1 h-3.5 w-3.5" />
                      </Button>
                    )}
                    <div className="flex items-center gap-1">
                      <Input
                        id={`ozon-margin-${key}`}
                        aria-label={`Наценка Ozon: ${option.name}`}
                        className="w-32"
                        type="text"
                        inputMode="decimal"
                        value={draft.margin}
                        maxLength={7}
                        disabled={!canManage || savingKey !== null}
                        onChange={(event) => updateDraft(option, { margin: event.target.value })}
                        placeholder="Наследовать"
                      />
                      <span className="text-sm text-muted-foreground">%</span>
                    </div>
                    {hasOwnSetting && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={!canManage || savingKey !== null}
                        onClick={() => resetPolicy(option)}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        <span className="sr-only">Сбросить наценку</span>
                      </Button>
                    )}
                    <Button
                      type="button"
                      size="sm"
                      disabled={!canManage || isSaving || savingKey !== null}
                      onClick={() => void savePolicy(option)}
                    >
                      {isSaving
                        ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                        : <CheckCircle2 className="mr-1 h-3.5 w-3.5" />}
                      {isSaving ? 'Сохраняем…' : 'Сохранить'}
                    </Button>
                  </div>
                )}
                {validationErrors[key] && (
                  <p className="text-xs text-destructive md:basis-full">{validationErrors[key]}</p>
                )}
              </div>
            );
          })}
        </div>
      )}

      <p className="text-[11px] text-muted-foreground">
        Показано {visibleOptions.length} из {level.options.length} элементов текущего раздела.
        Настройки сохраняются независимо для каждого кабинета Ozon.
      </p>
    </div>
  );
}
