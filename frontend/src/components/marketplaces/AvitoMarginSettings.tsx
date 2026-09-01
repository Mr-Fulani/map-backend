'use client';

import { useState } from 'react';
import { CheckCircle2, ChevronRight, Folder, Loader2, RotateCcw, Search, Tag } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { CatalogCategoryOption } from '@/components/products/catalog-category-picker';
import { productApi } from '@/lib/api';

function categoryPath(
  category: CatalogCategoryOption,
  byId: ReadonlyMap<number, CatalogCategoryOption>,
): CatalogCategoryOption[] {
  const result: CatalogCategoryOption[] = [];
  const seen = new Set<number>();
  let current: CatalogCategoryOption | undefined = category;
  while (current && !seen.has(current.id)) {
    result.unshift(current);
    seen.add(current.id);
    current = current.parent === null ? undefined : byId.get(current.parent);
  }
  return result;
}

export function AvitoMarginSettings({
  categories,
  onSaved,
}: {
  categories: CatalogCategoryOption[];
  onSaved: () => Promise<void>;
}) {
  const [search, setSearch] = useState('');
  const [parentId, setParentId] = useState<number | null>(null);
  const [values, setValues] = useState<Record<number, string>>(() => (
    Object.fromEntries(categories.map((category) => [
      category.id,
      category.default_margin_pct ?? '',
    ]))
  ));
  const [saving, setSaving] = useState<Record<number, boolean>>({});

  const handleSave = async (id: number, explicitValue?: string) => {
    const value = explicitValue ?? values[id] ?? '';
    setSaving((current) => ({ ...current, [id]: true }));
    try {
      await productApi.patchCatalogCategory(id, {
        default_margin_pct: value === '' ? null : value,
      });
      await onSaved();
      toast.success('Наценка сохранена');
    } catch {
      toast.error('Не удалось сохранить наценку');
    } finally {
      setSaving((current) => ({ ...current, [id]: false }));
    }
  };

  const byId = new Map(categories.map((category) => [category.id, category]));
  const currentParent = parentId === null ? null : byId.get(parentId) ?? null;
  const currentPath = currentParent ? categoryPath(currentParent, byId) : [];
  const categoryIdsWithChildren = new Set(
    categories.map((category) => category.parent).filter((id): id is number => id !== null),
  );
  const normalizedSearch = search.trim().toLocaleLowerCase('ru-RU');
  const rows = categories
    .filter((category) => {
      const visibleParent = category.parent !== null && byId.has(category.parent)
        ? category.parent
        : null;
      return visibleParent === parentId;
    })
    .filter((category) => (
      !normalizedSearch
      || category.name.toLocaleLowerCase('ru-RU').includes(normalizedSearch)
      || category.path_label.toLocaleLowerCase('ru-RU').includes(normalizedSearch)
    ))
    .sort((left, right) => left.name.localeCompare(right.name, 'ru-RU'));

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Наценки Avito для организации</p>
        <p className="mt-1">
          Пустое значение наследует наценку ближайшего родительского раздела.
          Цены товара и настройки других маркетплейсов не меняются.
        </p>
      </div>

      <nav aria-label="Путь категории наценок Avito" className="flex flex-wrap items-center gap-1 text-xs">
        <Button
          type="button"
          size="sm"
          variant={parentId === null ? 'secondary' : 'ghost'}
          onClick={() => { setParentId(null); setSearch(''); }}
        >
          Все категории
        </Button>
        {currentPath.map((item, index) => (
          <div key={item.id} className="flex items-center gap-1">
            <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
            <Button
              type="button"
              size="sm"
              variant={index === currentPath.length - 1 ? 'secondary' : 'ghost'}
              onClick={() => { setParentId(item.id); setSearch(''); }}
            >
              {item.name}
            </Button>
          </div>
        ))}
      </nav>

      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Фильтр текущего раздела наценок Avito"
          placeholder="Фильтр в текущем разделе"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="pl-9"
        />
      </div>

      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed p-4 text-center text-sm text-muted-foreground">
          {search ? 'В этом разделе ничего не найдено.' : 'В разделе нет вложенных категорий.'}
        </p>
      ) : (
        <div className="space-y-2">
          {rows.map((category) => {
            const hasChildren = categoryIdsWithChildren.has(category.id);
            return (
              <div
                key={category.id}
                className="flex flex-col gap-3 rounded-lg border bg-background p-3 md:flex-row md:items-center md:justify-between"
              >
                <div className="flex min-w-0 items-center gap-3">
                  <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-md border bg-muted/60">
                    {hasChildren
                      ? <Folder className="h-5 w-5 text-blue-500" />
                      : <Tag className="h-5 w-5 text-violet-500" />}
                  </div>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="break-words font-medium">{category.name}</p>
                      <Badge variant="outline">Правило Avito</Badge>
                    </div>
                    <p className="mt-0.5 break-words text-xs text-muted-foreground">
                      {category.path_label || category.path.join(' → ') || category.name}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Итог: {category.effective_margin_pct}%
                      {category.margin_inherited_from_name
                        ? ` · от «${category.margin_inherited_from_name}»`
                        : category.default_margin_pct == null
                          ? ' · без наценки'
                          : ' · собственная'}
                    </p>
                  </div>
                </div>
                <div className="flex w-full flex-col gap-2 md:w-auto md:min-w-[520px] md:flex-row md:items-center md:justify-end">
                  {hasChildren && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => { setParentId(category.id); setSearch(''); }}
                    >
                      Внутрь <ChevronRight className="ml-1 h-3.5 w-3.5" />
                    </Button>
                  )}
                  <div className="flex items-center gap-1">
                    <Input
                      value={values[category.id] ?? ''}
                      onChange={(event) => setValues((current) => ({
                        ...current,
                        [category.id]: event.target.value,
                      }))}
                      inputMode="decimal"
                      placeholder="Наследовать"
                      aria-label={`Наценка Avito: ${category.name}`}
                      className="w-32"
                    />
                    <span className="text-sm text-muted-foreground">%</span>
                  </div>
                  {category.default_margin_pct !== null && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={saving[category.id]}
                      onClick={() => {
                        setValues((current) => ({ ...current, [category.id]: '' }));
                        void handleSave(category.id, '');
                      }}
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                      <span className="sr-only">Наследовать наценку</span>
                    </Button>
                  )}
                  <Button
                    size="sm"
                    disabled={saving[category.id]}
                    onClick={() => void handleSave(category.id)}
                  >
                    {saving[category.id]
                      ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                      : <CheckCircle2 className="mr-1 h-3.5 w-3.5" />}
                    {saving[category.id] ? 'Сохраняем…' : 'Сохранить'}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        Показано {rows.length} элементов текущего раздела. Наценки Avito остаются
        общими для организации и не смешиваются с правилами кабинетов Ozon.
      </p>
    </div>
  );
}
