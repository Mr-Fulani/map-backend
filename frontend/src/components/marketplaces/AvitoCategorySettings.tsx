'use client';

import { useMemo, useState } from 'react';
import {
  AlertCircle,
  ChevronRight,
  Folder,
  Loader2,
  Search,
  Tag,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import type { CatalogCategoryOption } from '@/components/products/catalog-category-picker';
import { AVITO_CATEGORIES_LABEL } from '@/lib/marketplace-category-boundaries';

interface AvitoCategorySettingsProps {
  categories: CatalogCategoryOption[];
  loading: boolean;
  canManage: boolean;
  savingCategoryId: number | null;
  onToggle: (category: CatalogCategoryOption) => void;
}

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

export function AvitoCategorySettings({
  categories,
  loading,
  canManage,
  savingCategoryId,
  onToggle,
}: AvitoCategorySettingsProps) {
  const officialCategories = useMemo(
    () => categories.filter((category) => category.external_source === 'avito'),
    [categories],
  );
  const byId = useMemo(
    () => new Map(officialCategories.map((category) => [category.id, category])),
    [officialCategories],
  );
  const [parentId, setParentId] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const currentParent = parentId === null ? null : byId.get(parentId) ?? null;
  const currentPath = currentParent ? categoryPath(currentParent, byId) : [];
  const normalizedSearch = search.trim().toLocaleLowerCase('ru-RU');
  const visible = officialCategories
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
  const categoryIdsWithChildren = new Set(
    officialCategories
      .map((category) => category.parent)
      .filter((id): id is number => id !== null),
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>{AVITO_CATEGORIES_LABEL}</CardTitle>
          <Badge variant="outline">Официальное дерево</Badge>
          <Badge variant="outline">Общее для организации</Badge>
        </div>
        <CardDescription>
          Выбирайте доступные ветки Avito в том же формате, что и Ozon. Структура
          остаётся защищённой и продолжает использовать текущую проверенную логику Avito.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
          <p>
            MAP показывает здесь те же защищённые ветки, которые уже используются
            товарами и листингами Avito. Ничего не переносится и не дублируется.
          </p>
        </div>

        <nav aria-label="Путь категории Avito" className="flex flex-wrap items-center gap-1 text-xs">
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
            aria-label="Фильтр текущего раздела Avito"
            className="pl-9"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Фильтр в текущем разделе"
          />
        </div>

        {loading ? (
          <div className="h-28 animate-pulse rounded-md bg-muted" />
        ) : officialCategories.length === 0 ? (
          <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
            Официальное дерево Avito пока не загружено для организации.
          </p>
        ) : visible.length === 0 ? (
          <p className="rounded-md border border-dashed p-4 text-center text-sm text-muted-foreground">
            {search.trim() ? 'В этом разделе ничего не найдено.' : 'В разделе нет вложенных категорий.'}
          </p>
        ) : (
          <div className="space-y-2">
            {visible.map((category) => {
              const hasChildren = categoryIdsWithChildren.has(category.id);
              const isSaving = savingCategoryId === category.id;
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
                        <Badge variant="outline">Официальная Avito</Badge>
                      </div>
                      <p className="mt-0.5 break-words text-xs text-muted-foreground">
                        {category.path_label || category.path.join(' → ') || category.name}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Статус: {category.is_active ? 'включена для ассортимента' : 'отключена'}
                      </p>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 md:justify-end">
                    {hasChildren && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={savingCategoryId !== null}
                        onClick={() => { setParentId(category.id); setSearch(''); }}
                      >
                        Открыть ветку <ChevronRight className="ml-1 h-3.5 w-3.5" />
                      </Button>
                    )}
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!canManage || savingCategoryId !== null}
                      onClick={() => onToggle(category)}
                    >
                      {isSaving && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
                      {category.is_active ? 'Отключить' : 'Включить'}
                    </Button>
                    <Badge variant={category.is_active ? 'default' : 'secondary'}>
                      {category.is_active ? 'Активна' : 'Отключена'}
                    </Badge>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <p className="text-[11px] text-muted-foreground">
          Настройка применяется ко всем кабинетам Avito организации. Изменения структуры
          и существующие обработчики публикации Avito не затрагиваются.
        </p>
      </CardContent>
    </Card>
  );
}
