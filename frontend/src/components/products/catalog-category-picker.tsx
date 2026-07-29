'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, FolderTree, Search } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

export interface CatalogCategoryOption {
  id: number;
  name: string;
  parent: number | null;
  root_domain: number | null;
  root_domain_slug: string;
  root_domain_name: string;
  domain: string;
  aliases: string[];
  external_source: string;
  is_active: boolean;
  path: string[];
  path_label: string;
  depth: number;
  has_active_children: boolean;
  is_selectable: boolean;
}

interface CatalogCategoryPickerProps {
  categories: CatalogCategoryOption[];
  value: string;
  onValueChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
}

function sourcePriority(category: CatalogCategoryOption): number {
  if (category.external_source === 'avito') return 0;
  if (!category.external_source) return 1;
  return 2;
}

function categoryPath(category: CatalogCategoryOption): string {
  return category.path_label || category.path?.join(' / ') || category.name;
}

export function CatalogCategoryPicker({
  categories,
  value,
  onValueChange,
  disabled = false,
  placeholder = 'Выберите категорию',
  className,
}: CatalogCategoryPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const activeCategories = useMemo(
    () => categories.filter((category) => category.is_active),
    [categories],
  );
  const selected = activeCategories.find((category) => String(category.id) === value);

  const visibleCategories = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase('ru-RU');
    return [...activeCategories]
      .filter((category) => {
        if (!normalizedQuery) return true;
        const haystack = [
          category.name,
          categoryPath(category),
          category.root_domain_name,
          ...(category.aliases ?? []),
        ].join(' ').toLocaleLowerCase('ru-RU');
        return haystack.includes(normalizedQuery);
      })
      .sort((left, right) => {
        const domainOrder = (left.root_domain_name || left.domain).localeCompare(
          right.root_domain_name || right.domain,
          'ru',
        );
        if (domainOrder !== 0) return domainOrder;
        const sourceOrder = sourcePriority(left) - sourcePriority(right);
        if (sourceOrder !== 0) return sourceOrder;
        return categoryPath(left).localeCompare(categoryPath(right), 'ru');
      });
  }, [activeCategories, query]);

  useEffect(() => {
    function handleOutsideClick(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleOutsideClick);
    return () => document.removeEventListener('mousedown', handleOutsideClick);
  }, []);

  useEffect(() => {
    if (open) {
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  let previousDomain = '';
  let previousSource = '';

  return (
    <div ref={rootRef} className={cn('relative', className)}>
      <Button
        type="button"
        variant="outline"
        className="h-auto min-h-9 w-full justify-between gap-2 px-3 py-2 text-left font-normal"
        onClick={() => setOpen((current) => !current)}
        disabled={disabled}
        aria-expanded={open}
      >
        <span className={cn('min-w-0 flex-1 truncate', !selected && 'text-muted-foreground')}>
          {selected ? categoryPath(selected) : value ? 'Уточните категорию' : placeholder}
        </span>
        <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
      </Button>

      {value && (!selected || !selected.is_selectable) && (
        <p className="mt-1 text-xs text-amber-600">
          Текущая категория является разделом или больше недоступна. Уточните конечную подкатегорию.
        </p>
      )}

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-full min-w-[20rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md sm:min-w-[34rem]">
          <div className="border-b p-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                ref={inputRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') setOpen(false);
                }}
                placeholder="Название, синоним или путь категории"
                className="pl-8"
              />
            </div>
          </div>

          <div className="max-h-80 overflow-y-auto p-1">
            {visibleCategories.length === 0 ? (
              <p className="px-3 py-8 text-center text-sm text-muted-foreground">
                Категории не найдены
              </p>
            ) : (
              visibleCategories.map((category) => {
                const domain = category.root_domain_name || category.domain;
                const source = category.external_source === 'avito'
                  ? 'Категории Avito'
                  : category.external_source
                    ? 'Категории каталога'
                    : 'Собственные категории';
                const showDomain = domain !== previousDomain;
                const showSource = showDomain || source !== previousSource;
                previousDomain = domain;
                previousSource = source;
                const isSelected = String(category.id) === value;

                return (
                  <div key={category.id}>
                    {showDomain && (
                      <p className="px-2 pb-1 pt-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground first:pt-2">
                        {domain}
                      </p>
                    )}
                    {showSource && (
                      <p className="px-2 py-1 text-xs font-medium text-primary">
                        {source}
                      </p>
                    )}
                    <button
                      type="button"
                      className={cn(
                        'flex w-full items-start gap-2 rounded-sm px-2 py-2 text-left text-sm',
                        category.is_selectable
                          ? 'hover:bg-accent hover:text-accent-foreground'
                          : 'cursor-default font-medium text-muted-foreground',
                      )}
                      style={query ? undefined : { paddingLeft: `${8 + category.depth * 18}px` }}
                      disabled={!category.is_selectable}
                      title={categoryPath(category)}
                      onClick={() => {
                        onValueChange(String(category.id));
                        setOpen(false);
                        setQuery('');
                      }}
                    >
                      {category.is_selectable ? (
                        <span className="mt-0.5 h-4 w-4 shrink-0">
                          {isSelected && <Check className="h-4 w-4 text-primary" />}
                        </span>
                      ) : (
                        <FolderTree className="mt-0.5 h-4 w-4 shrink-0" />
                      )}
                      <span className="min-w-0">
                        <span className="block">{category.name}</span>
                        {query && (
                          <span className="block truncate text-xs font-normal text-muted-foreground">
                            {categoryPath(category)}
                          </span>
                        )}
                      </span>
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
