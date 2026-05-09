/**
 * Шаг 4: Маппинг категорий 1С → Avito.
 * Показываем список категорий из импортированных товаров,
 * пользователь выбирает соответствующую категорию Avito.
 */

'use client';

import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import { categoryApi } from '@/lib/api';
import { ArrowLeft, ArrowRight, Loader2, Tag, AlertCircle } from 'lucide-react';
import { toast } from 'sonner';

interface StepCategoriesProps {
  data: Record<string, unknown>;
  onNext: (data?: Record<string, unknown>) => void;
  onBack: () => void;
}

interface UnmappedCategory {
  category_source: string;
  count: number;
}

// Основные категории Avito для автозапчастей
const AVITO_CATEGORIES = [
  { id: 1, name: 'Запчасти' },
  { id: 2, name: 'Аксессуары' },
  { id: 3, name: 'Шины, диски и колёса' },
  { id: 4, name: 'Масла и автохимия' },
  { id: 5, name: 'Инструменты' },
  { id: 6, name: 'Тюнинг' },
  { id: 7, name: 'Аудио и видео' },
  { id: 8, name: 'Прочее' },
];

export function StepCategories({ onNext, onBack }: StepCategoriesProps) {
  const [unmapped, setUnmapped] = useState<UnmappedCategory[]>([]);
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    loadUnmapped();
  }, []);

  async function loadUnmapped() {
    try {
      const { data: result } = await categoryApi.getUnmapped();
      const items = result.unmapped || [];
      setUnmapped(items);
      // Pre-fill default mapping (all as "Запчасти" for auto parts)
      const defaults: Record<string, string> = {};
      items.forEach((cat: UnmappedCategory) => {
        defaults[cat.category_source] = '1'; // Default: Запчасти
      });
      setMappings(defaults);
    } catch {
      // If no unmapped categories, just show empty state
      setUnmapped([]);
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSave() {
    setIsSaving(true);
    try {
      // Save each mapping
      const promises = Object.entries(mappings).map(([source, targetId]) => {
        const target = AVITO_CATEGORIES.find((c) => c.id.toString() === targetId);
        return categoryApi.createMapping({
          marketplace: 'avito',
          category_source: source,
          category_target: target?.name || '',
          category_id: parseInt(targetId),
        });
      });

      await Promise.allSettled(promises);
      toast.success('Категории сохранены!');
      onNext({ categories_mapped: true });
    } catch {
      toast.error('Ошибка сохранения категорий');
    } finally {
      setIsSaving(false);
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        {[1, 2, 3].map((i) => (
          <div key={i} className="flex items-center gap-4">
            <Skeleton className="h-10 flex-1" />
            <Skeleton className="h-10 w-48" />
          </div>
        ))}
      </div>
    );
  }

  if (unmapped.length === 0) {
    return (
      <div className="space-y-6">
        <div className="flex flex-col items-center gap-3 py-8 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-yellow-500/10">
            <AlertCircle className="h-6 w-6 text-yellow-500" />
          </div>
          <p className="text-sm text-muted-foreground">
            Нет импортированных товаров с категориями.
            <br />
            Вы можете настроить маппинг позже в Настройках.
          </p>
        </div>
        <div className="flex justify-between">
          <Button type="button" variant="outline" onClick={onBack}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Назад
          </Button>
          <Button onClick={() => onNext({ categories_mapped: false })}>
            Пропустить
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3">
        <p className="text-xs text-blue-500">
          <Tag className="mr-1.5 inline h-3.5 w-3.5" />
          Укажите, какой категории Avito соответствует каждая категория из вашего каталога.
          Мы предзаполнили «Запчасти» по умолчанию.
        </p>
      </div>

      <div className="space-y-3">
        {unmapped.map((cat) => (
          <div
            key={cat.category_source}
            className="flex items-center gap-4 rounded-lg border p-3"
          >
            <div className="flex-1">
              <p className="text-sm font-medium">{cat.category_source}</p>
              <Badge variant="secondary" className="mt-1 text-xs">
                {cat.count} товаров
              </Badge>
            </div>
            <div className="w-48">
              <Label className="sr-only">Категория Avito</Label>
              <Select
                value={mappings[cat.category_source] || ''}
                onValueChange={(value) =>
                  setMappings((prev) => ({
                    ...prev,
                    [cat.category_source]: value,
                  }))
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="Выберите..." />
                </SelectTrigger>
                <SelectContent>
                  {AVITO_CATEGORIES.map((ac) => (
                    <SelectItem key={ac.id} value={ac.id.toString()}>
                      {ac.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        ))}
      </div>

      <div className="flex justify-between">
        <Button type="button" variant="outline" onClick={onBack}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Назад
        </Button>
        <Button onClick={handleSave} disabled={isSaving}>
          {isSaving ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Сохранение...
            </>
          ) : (
            <>
              Сохранить и продолжить
              <ArrowRight className="ml-2 h-4 w-4" />
            </>
          )}
        </Button>
      </div>
    </div>
  );
}
