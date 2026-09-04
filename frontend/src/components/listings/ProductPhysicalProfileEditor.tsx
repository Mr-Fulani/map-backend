'use client';

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import { CheckCircle2, ExternalLink, Loader2, Save } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { productApi } from '@/lib/api';
import {
  canonicalPhysicalValueToDisplay,
  effectivePhysicalValueForInput,
  physicalDraftFromProfile,
  physicalDraftToApiPayload,
  physicalSuggestionIsAlreadyUsed,
  physicalSuggestionNeedsReview,
  OPTIONAL_FOR_OZON_IMPORT,
  PRODUCT_PHYSICAL_GUIDANCE,
  PRODUCT_PHYSICAL_FIELDS,
  type ProductPhysicalFieldKey,
  type ProductPhysicalProfile,
} from '@/lib/product-physical-profile';

export interface ProductPhysicalProfileEditorHandle {
  focusField: (field: string) => boolean;
}

interface Props {
  productId: number;
  profile: ProductPhysicalProfile;
  onProfileChange: (profile: ProductPhysicalProfile) => void;
  title?: string;
}

export const ProductPhysicalProfileEditor = forwardRef<
ProductPhysicalProfileEditorHandle,
Props
>(function ProductPhysicalProfileEditor({
  productId,
  profile,
  onProfileChange,
  title = '4. Упаковка и налог',
}, ref) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState(() => physicalDraftFromProfile(profile));
  const [saving, setSaving] = useState(false);
  const [suggestionActionId, setSuggestionActionId] = useState<number | null>(null);
  const requiredMissing = profile.missing_fields.filter((field) => (
    !OPTIONAL_FOR_OZON_IMPORT.has(field)
  ));
  const fieldsToConfirm = new Set<ProductPhysicalFieldKey>(
    profile.suggestions
      .filter((suggestion) => physicalSuggestionNeedsReview(profile, suggestion))
      .map((suggestion) => suggestion.field),
  );
  const fieldsToSource = requiredMissing.filter((field) => !fieldsToConfirm.has(field));
  const automaticFields = PRODUCT_PHYSICAL_FIELDS.filter(({ key }) => {
    const fact = profile.facts[key];
    return fact.effective_source === '1c'
      || (fact.effective_source === 'map' && Boolean(fact.map_provenance));
  });
  const manualFields = PRODUCT_PHYSICAL_FIELDS.filter(({ key }) => {
    const fact = profile.facts[key];
    return fact.effective_source === 'map' && !fact.map_provenance;
  });

  useEffect(() => {
    setDraft(physicalDraftFromProfile(profile));
  }, [profile]);

  useImperativeHandle(ref, () => ({
    focusField: (field: string) => {
      if (!field.startsWith('physical:')) return false;
      const key = field.slice('physical:'.length);
      const target = rootRef.current?.querySelector<HTMLElement>(`[data-physical-field="${key}"]`);
      if (!target) return false;
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.querySelector<HTMLElement>('input, button, [role="combobox"]')?.focus({
        preventScroll: true,
      });
      return true;
    },
  }));

  function setField(field: ProductPhysicalFieldKey, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  async function saveProfile() {
    setSaving(true);
    try {
      const payload = physicalDraftToApiPayload(draft);
      const response = await productApi.updatePhysicalProfile(productId, payload);
      const next = response.data.data as ProductPhysicalProfile;
      onProfileChange(next);
      toast.success('Упаковка и налог сохранены в MAP.');
    } catch (error: unknown) {
      const apiMessage = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(apiMessage ?? (error instanceof Error ? error.message : 'Не удалось сохранить данные.'));
    } finally {
      setSaving(false);
    }
  }

  async function reviewSuggestion(
    suggestionId: number,
    action: 'approve' | 'reject',
  ) {
    setSuggestionActionId(suggestionId);
    try {
      const response = await productApi.reviewPhysicalSuggestion(
        productId,
        suggestionId,
        action,
      );
      onProfileChange(response.data.data as ProductPhysicalProfile);
      toast.success(action === 'approve' ? 'Значение принято и записано в MAP.' : 'Вариант отклонён.');
    } catch (error: unknown) {
      const code = (error as { response?: { data?: { code?: string } } }).response?.data?.code;
      toast.error(
        code === 'source_value_preferred'
          ? 'Поле уже заполнено корректным значением из 1С.'
          : 'Не удалось сохранить решение по найденному значению.',
      );
    } finally {
      setSuggestionActionId(null);
    }
  }

  return (
    <div
      ref={rootRef}
      data-testid="ozon-physical-profile-section"
      className={`space-y-4 rounded-lg border border-l-4 p-4 ${
        requiredMissing.length > 0
          ? 'border-amber-500/50 bg-amber-500/5'
          : 'border-emerald-500/35 bg-emerald-500/5'
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">{title}</p>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            MAP использует данные 1С и найденные факты. Если их нет, заполните значение
            здесь. Штрихкод и НДС можно оставить пустыми для создания карточки.
          </p>
        </div>
        <Badge
          variant="outline"
          className={requiredMissing.length > 0
            ? 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'
            : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200'}
        >
          {requiredMissing.length > 0 ? `Нужно заполнить: ${requiredMissing.length}` : 'Готово'}
        </Badge>
      </div>

      <div className="grid gap-2 text-xs sm:grid-cols-2">
        <div className="rounded-md border border-emerald-500/35 bg-emerald-500/10 p-2.5 text-emerald-950 dark:text-emerald-100">
          <strong className="block">MAP или 1С заполнили: {automaticFields.length}</strong>
          Источник подтверждён — переписывать значение не требуется.
        </div>
        <div className="rounded-md border border-blue-500/35 bg-blue-500/10 p-2.5 text-blue-950 dark:text-blue-100">
          <strong className="block">
            Подтвердить найденное: {fieldsToConfirm.size} · Введено вручную: {manualFields.length}
          </strong>
          Синие поля требуют проверки Тенанта, даже если значение уже сохранено.
        </div>
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-amber-950 dark:text-amber-100 sm:col-span-2">
          <strong className="block">Нужно получить или измерить: {fieldsToSource.length}</strong>
          MAP не нашёл надёжного источника. Под каждым полем написано, где взять значение.
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {PRODUCT_PHYSICAL_FIELDS.map(({ key, label, unit, placeholder }) => {
          const fact = profile.facts[key];
          const from1c = fact.effective_source === '1c';
          const fromEvidence = fact.effective_source === 'map' && Boolean(fact.map_provenance);
          const enteredManually = fact.effective_source === 'map' && !fact.map_provenance;
          const optional = OPTIONAL_FOR_OZON_IMPORT.has(key);
          const missing = fact.effective_source === 'missing';
          const guidance = PRODUCT_PHYSICAL_GUIDANCE[key];
          const suggestions = profile.suggestions.filter((item) => item.field === key);
          const reviewSuggestions = suggestions.filter((item) => (
            physicalSuggestionNeedsReview(profile, item)
          ));
          const value = effectivePhysicalValueForInput(profile, key, draft);
          const stateClass = reviewSuggestions.length > 0
            ? 'border-blue-500/40 bg-blue-500/5'
            : enteredManually
              ? 'border-blue-500/35 bg-blue-500/5'
            : missing && !optional
              ? 'border-amber-500/50 bg-amber-500/5'
              : missing
                ? 'border-dashed bg-muted/20'
                : 'border-emerald-500/35 bg-background';

          return (
            <div
              key={key}
              data-physical-field={key}
              className={`space-y-2 rounded-lg border border-l-4 p-3 ${stateClass}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <label htmlFor={`drawer-physical-${key}`} className="text-sm font-medium">
                  {label}{unit ? `, ${unit}` : ''}
                </label>
                <Badge
                  variant="outline"
                  className={from1c || fact.effective_source === 'map'
                    ? enteredManually
                      ? 'border-blue-500/40 bg-blue-500/10 text-blue-900 dark:text-blue-100'
                      : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200'
                    : reviewSuggestions.length > 0
                      ? 'border-blue-500/40 bg-blue-500/10 text-blue-900 dark:text-blue-100'
                      : optional
                        ? 'border-dashed text-muted-foreground'
                        : 'border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-100'}
                >
                  {from1c
                    ? 'Готово · из 1С'
                    : fromEvidence
                      ? 'Готово · подтверждено'
                      : enteredManually
                        ? 'Сохранено · проверить'
                      : reviewSuggestions.length > 0
                        ? 'Подтвердить'
                        : optional ? 'Необязательно' : 'Заполнить'}
                </Badge>
              </div>

              {key === 'vat_rate' ? (
                <Select
                  value={value || 'not_set'}
                  onValueChange={(next) => setField(key, next === 'not_set' ? '' : next)}
                  disabled={from1c || saving}
                >
                  <SelectTrigger id={`drawer-physical-${key}`}>
                    <SelectValue placeholder="Выберите ставку" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="not_set">Не указано</SelectItem>
                    {['0', '5', '7', '10', '20'].map((rate) => (
                      <SelectItem key={rate} value={rate}>{rate}%</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id={`drawer-physical-${key}`}
                  value={value}
                  placeholder={placeholder}
                  inputMode={key === 'barcode' ? 'text' : 'decimal'}
                  disabled={from1c || saving}
                  onChange={(event) => setField(key, event.target.value)}
                />
              )}

              <p className="text-xs text-muted-foreground">
                {from1c
                  ? 'Используется автоматически; вручную менять не нужно.'
                  : fromEvidence
                    ? 'MAP использует значение, которое Тенант подтвердил из найденного источника.'
                    : enteredManually
                      ? 'Значение введено вручную. MAP сохранил его, но не может подтвердить достоверность.'
                    : optional && missing
                    ? key === 'barcode'
                      ? 'Можно пропустить: после создания карточки MAP предложит код Ozon.'
                      : 'Не знаете ставку — оставьте «Не указано».'
                    : missing
                      ? 'Нет корректного значения в 1С или MAP.'
                      : 'Значение сохранено в MAP и используется для Ozon.'}
              </p>

              {!from1c && (
                <div className="rounded-md border bg-background/80 p-2 text-xs leading-relaxed">
                  <p><strong>Где взять:</strong> {guidance.source}</p>
                  <p className="mt-1 text-muted-foreground">{guidance.warning}</p>
                </div>
              )}

              {fact.source_error && (
                <p className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-950 dark:text-amber-100">
                  <strong>1С передала некорректное значение:</strong> {fact.source_error}
                </p>
              )}

              {fact.map_provenance && (
                <p className="text-xs text-muted-foreground">
                  Подтверждённый источник: {fact.map_provenance.source_label}
                  {fact.map_provenance.source_url && (
                    <a
                      href={fact.map_provenance.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="ml-1 inline-flex items-center text-primary hover:underline"
                    >
                      открыть <ExternalLink className="ml-1 h-3 w-3" />
                    </a>
                  )}
                </p>
              )}

              {reviewSuggestions.map((suggestion) => (
                <div
                  key={suggestion.id}
                  className="space-y-2 rounded-md border border-blue-500/30 bg-blue-500/10 p-2.5 text-xs"
                >
                  <p className="font-medium">
                    MAP нашёл: {canonicalPhysicalValueToDisplay(key, suggestion.value)}{unit ? ` ${unit}` : ''}
                  </p>
                  <p className="text-muted-foreground">
                    {suggestion.source_label} · {suggestion.raw_name}: {suggestion.raw_value}
                  </p>
                  {suggestion.source_url && (
                    <a
                      href={suggestion.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center text-primary hover:underline"
                    >
                      Проверить в источнике <ExternalLink className="ml-1 h-3 w-3" />
                    </a>
                  )}
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      size="sm"
                      className="h-7"
                      disabled={suggestionActionId !== null || saving}
                      onClick={() => void reviewSuggestion(suggestion.id, 'approve')}
                    >
                      {suggestionActionId === suggestion.id && (
                        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                      )}
                      Принять значение
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="h-7"
                      disabled={suggestionActionId !== null || saving}
                      onClick={() => void reviewSuggestion(suggestion.id, 'reject')}
                    >
                      Отклонить
                    </Button>
                  </div>
                </div>
              ))}

              {suggestions.some((item) => physicalSuggestionIsAlreadyUsed(profile, item)) && (
                <p className="flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-300">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Найденное значение уже подтверждено.
                </p>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex flex-col gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-muted-foreground">
          Размеры вводятся в сантиметрах, вес — в килограммах.
        </p>
        <Button type="button" onClick={() => void saveProfile()} disabled={saving}>
          {saving
            ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            : <Save className="mr-2 h-4 w-4" />}
          Сохранить упаковку
        </Button>
      </div>
    </div>
  );
});
