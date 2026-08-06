'use client';

import { Check, Globe2, MapPin } from 'lucide-react';

import { Button } from '@/components/ui/button';

export type ResearchRegionPreset = 'russia' | 'russia_cis' | 'custom' | 'worldwide';

export const CIS_COUNTRIES = [
  ['RU', 'Россия'], ['BY', 'Беларусь'], ['KZ', 'Казахстан'],
  ['AM', 'Армения'], ['KG', 'Кыргызстан'], ['UZ', 'Узбекистан'],
  ['AZ', 'Азербайджан'], ['MD', 'Молдова'], ['TJ', 'Таджикистан'],
] as const;

export const OTHER_COUNTRIES = [
  ['GE', 'Грузия'], ['TR', 'Турция'], ['DE', 'Германия'], ['PL', 'Польша'],
  ['CZ', 'Чехия'], ['LT', 'Литва'], ['LV', 'Латвия'], ['EE', 'Эстония'],
  ['CN', 'Китай'], ['KR', 'Южная Корея'], ['JP', 'Япония'], ['AE', 'ОАЭ'],
  ['US', 'США'], ['GB', 'Великобритания'], ['FR', 'Франция'], ['IT', 'Италия'],
  ['ES', 'Испания'], ['NL', 'Нидерланды'], ['UA', 'Украина'],
] as const;

export const RESEARCH_COUNTRY_LABELS: Record<string, string> = Object.fromEntries([
  ...CIS_COUNTRIES,
  ...OTHER_COUNTRIES,
]);

const REGION_OPTIONS: Array<{
  value: ResearchRegionPreset;
  title: string;
  description: string;
}> = [
  { value: 'russia', title: 'Россия', description: 'Искать российских продавцов' },
  { value: 'russia_cis', title: 'Россия и СНГ', description: 'Отдельный поиск по 9 странам' },
  { value: 'custom', title: 'Выбранные страны', description: 'Собрать свой список стран' },
  { value: 'worldwide', title: 'Весь мир', description: 'Искать без ограничения по стране' },
];

export function countryCodesForPreset(
  preset: ResearchRegionPreset,
  currentCountryCodes: string[],
) {
  if (preset === 'russia') return ['RU'];
  if (preset === 'russia_cis') return CIS_COUNTRIES.map(([code]) => code);
  if (preset === 'worldwide') return [];
  return currentCountryCodes.length > 0 ? currentCountryCodes : ['RU'];
}

export function ResearchGeographyPicker({
  regionPreset,
  countryCodes,
  canEdit,
  saving = false,
  compact = false,
  onChange,
}: {
  regionPreset: ResearchRegionPreset;
  countryCodes: string[];
  canEdit: boolean;
  saving?: boolean;
  compact?: boolean;
  onChange: (regionPreset: ResearchRegionPreset, countryCodes: string[]) => void;
}) {
  const selectRegion = (preset: ResearchRegionPreset) => {
    onChange(preset, countryCodesForPreset(preset, countryCodes));
  };

  const toggleCountry = (code: string) => {
    const next = countryCodes.includes(code)
      ? countryCodes.filter((item) => item !== code)
      : [...countryCodes, code];
    if (next.length > 0) onChange('custom', next);
  };

  return (
    <div className={`rounded-xl border bg-card/70 ${compact ? 'p-3' : 'p-4'}`}>
      <div className="flex items-start gap-3">
        <span className="mt-0.5 rounded-lg bg-primary/10 p-2 text-primary">
          <Globe2 className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <p className="text-sm font-medium">География нового поиска</p>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            Выберите, в каких странах искать цены. Настройка сохранится для вашей организации.
          </p>
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2 2xl:grid-cols-4">
        {REGION_OPTIONS.map((option) => {
          const selected = option.value === regionPreset;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={selected}
              disabled={!canEdit || saving}
              onClick={() => selectRegion(option.value)}
              className={`relative min-h-[72px] rounded-lg border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                selected
                  ? 'border-primary bg-primary/5 ring-1 ring-primary/20'
                  : 'bg-background hover:border-primary/40 hover:bg-muted/30'
              }`}
            >
              <span className="block pr-6 text-sm font-medium">{option.title}</span>
              <span className="mt-1 block text-[11px] leading-snug text-muted-foreground">
                {option.description}
              </span>
              {selected && (
                <span className="absolute right-2.5 top-2.5 rounded-full bg-primary p-0.5 text-primary-foreground">
                  <Check className="h-3 w-3" />
                </span>
              )}
            </button>
          );
        })}
      </div>

      {regionPreset === 'russia_cis' && (
        <div className="mt-3 rounded-lg border bg-muted/20 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <MapPin className="h-3.5 w-3.5 text-primary" />
            Страны поиска
          </p>
          <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
            {CIS_COUNTRIES.map(([, label]) => label).join(', ')}.
          </p>
        </div>
      )}

      {regionPreset === 'custom' && (
        <div className="mt-3 space-y-4 rounded-lg border bg-muted/20 p-3">
          <CountryGroup
            title="Россия и страны СНГ"
            countries={CIS_COUNTRIES}
            selectedCodes={countryCodes}
            disabled={!canEdit || saving}
            onToggle={toggleCountry}
          />
          <CountryGroup
            title="Другие страны"
            countries={OTHER_COUNTRIES}
            selectedCodes={countryCodes}
            disabled={!canEdit || saving}
            onToggle={toggleCountry}
          />
          <p className="text-[11px] text-muted-foreground">
            Выбрано стран: {countryCodes.length}. Нельзя убрать последнюю выбранную страну.
          </p>
        </div>
      )}

      {!canEdit && (
        <p className="mt-3 text-xs text-muted-foreground">
          Изменять географию могут владелец и администратор организации.
        </p>
      )}
    </div>
  );
}

function CountryGroup({
  title,
  countries,
  selectedCodes,
  disabled,
  onToggle,
}: {
  title: string;
  countries: ReadonlyArray<readonly [string, string]>;
  selectedCodes: string[];
  disabled: boolean;
  onToggle: (code: string) => void;
}) {
  return (
    <div>
      <p className="text-xs font-medium">{title}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {countries.map(([code, label]) => {
          const selected = selectedCodes.includes(code);
          return (
            <Button
              key={code}
              type="button"
              size="sm"
              variant="outline"
              aria-pressed={selected}
              disabled={disabled}
              onClick={() => onToggle(code)}
              className={`h-8 rounded-full px-3 text-xs ${
                selected
                  ? 'border-primary bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground'
                  : 'bg-background'
              }`}
            >
              {selected && <Check className="mr-1.5 h-3 w-3" />}
              {label}
            </Button>
          );
        })}
      </div>
    </div>
  );
}
