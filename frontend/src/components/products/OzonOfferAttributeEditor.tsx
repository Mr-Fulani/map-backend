'use client';

import { AlertCircle, CheckCircle2, Loader2, ShieldCheck } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  isOzonBooleanAttribute,
  ozonAttributeValidationMessage,
  type OzonAutofillField,
  type OzonAutofillRecommendation,
  type OzonDictionaryValue,
  type OzonOfferAttribute,
  type OzonOfferValue,
} from '@/lib/ozon-offer-preparation';

interface Props {
  attribute: OzonOfferAttribute;
  autofill?: OzonAutofillField;
  recommendation?: OzonAutofillRecommendation;
  dictionaryQuery: string;
  dictionaryResults: OzonDictionaryValue[];
  dictionaryLoading: boolean;
  onDictionaryQueryChange: (value: string) => void;
  onDictionarySearch: () => void;
  onValueChange: (value: OzonOfferValue | null) => void;
}

export function OzonOfferAttributeEditor({
  attribute,
  autofill,
  recommendation,
  dictionaryQuery,
  dictionaryResults,
  dictionaryLoading,
  onDictionaryQueryChange,
  onDictionarySearch,
  onValueChange,
}: Props) {
  const selected = attribute.selected_values[0];
  const validationMessage = ozonAttributeValidationMessage(attribute);
  const hasInvalidValue = Boolean(validationMessage);
  const hasValidValue = Boolean(selected) && !hasInvalidValue;
  const needsRequiredValue = attribute.is_required && !hasValidValue;
  const booleanAttribute = isOzonBooleanAttribute(attribute);

  return (
    <div
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
                onClick={() => onValueChange({ value, dictionary_value_id: 0 })}
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
              <Button type="button" size="sm" variant="ghost" onClick={() => onValueChange(null)}>
                Очистить
              </Button>
            </div>
          )}
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              aria-label={`Поиск: ${attribute.name}`}
              className={needsRequiredValue ? 'border-amber-500/50 bg-background' : undefined}
              value={dictionaryQuery}
              onChange={(event) => onDictionaryQueryChange(event.target.value)}
              placeholder="Введите название значения"
            />
            <Button
              type="button"
              variant="outline"
              onClick={onDictionarySearch}
              disabled={dictionaryLoading}
            >
              {dictionaryLoading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Найти в Ozon
            </Button>
          </div>
          {dictionaryResults.slice(0, 10).map((value) => (
            <button
              type="button"
              key={value.id}
              className="block w-full rounded border p-2 text-left text-sm hover:bg-muted/50"
              onClick={() => onValueChange({ value: value.value, dictionary_value_id: value.id })}
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
          onChange={(event) => onValueChange(
            event.target.value.trim()
              ? { value: event.target.value, dictionary_value_id: 0 }
              : null,
          )}
          placeholder="Введите значение"
        />
      )}
    </div>
  );
}
