'use client';

import { useEffect, useState } from 'react';
import { Globe2, Loader2, SearchCheck } from 'lucide-react';
import { toast } from 'sonner';

import { webResearchApi } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
  ResearchGeographyPicker,
  type ResearchRegionPreset,
} from '@/components/settings/research-geography-picker';

interface ResearchSettings {
  market_research_enabled: boolean;
  region_preset: ResearchRegionPreset;
  region_label: string;
  country_codes: string[];
  search_language: string;
  include_marketplaces: boolean;
  include_used: boolean;
  include_preorder: boolean;
  include_analogues: boolean;
  exact_matches_only: boolean;
  result_limit: number;
  price_ttl_hours: number;
  display_currency: string;
  updated_at: string;
}

function ToggleRow({
  title, description, checked, onCheckedChange, disabled,
}: {
  title: string;
  description: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className={`flex items-start justify-between gap-4 rounded-lg border p-3 transition-colors ${checked ? 'border-primary/35 bg-primary/5' : 'bg-card'}`}>
      <div className="min-w-0">
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        <p className={`mt-1 text-[11px] font-medium ${checked ? 'text-primary' : 'text-muted-foreground'}`}>
          {checked ? 'Включено' : 'Выключено'}
        </p>
      </div>
      <Switch aria-label={title} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} />
    </div>
  );
}

export function WebResearchSettingsCard() {
  const [settings, setSettings] = useState<ResearchSettings | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedFeedback, setSavedFeedback] = useState(false);

  useEffect(() => {
    webResearchApi.settings()
      .then((response) => {
        setSettings(response.data.data);
        setCanEdit(Boolean(response.data.can_edit));
      })
      .catch(() => toast.error('Не удалось загрузить настройки исследования'))
      .finally(() => setLoading(false));
  }, []);

  if (loading || !settings) {
    return <Skeleton className="h-[520px] w-full rounded-xl" />;
  }

  const persist = async (next: ResearchSettings): Promise<boolean> => {
    const previous = settings;
    setSettings(next);
    setSaving(true);
    setSavedFeedback(false);
    try {
      const response = await webResearchApi.updateSettings(next);
      setSettings(response.data.data);
      setSavedFeedback(true);
      window.setTimeout(() => setSavedFeedback(false), 2500);
      return true;
    } catch (error: unknown) {
      setSettings(previous);
      const data = (error as { response?: { data?: Record<string, unknown> } })?.response?.data;
      const detail = typeof data?.detail === 'string' ? data.detail : 'Проверьте выбранные значения';
      toast.error(detail);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const updateAndSave = <K extends keyof ResearchSettings>(key: K, value: ResearchSettings[K]) => {
    void persist({ ...settings, [key]: value });
  };

  const changeGeography = (region: ResearchRegionPreset, countryCodes: string[]) => {
    void persist({ ...settings, region_preset: region, country_codes: countryCodes });
  };

  const save = async () => {
    if (await persist(settings)) toast.success('Настройки интернет-исследования сохранены');
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Globe2 className="h-5 w-5" />
              Интернет-исследование рынка
            </CardTitle>
            <CardDescription className="mt-1 max-w-3xl">
              Эти правила применяются только к вашей организации. API-ключи и выбор поискового
              сервиса настраивает администратор платформы.
            </CardDescription>
          </div>
          {!canEdit && <Badge variant="secondary">Только просмотр</Badge>}
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <ToggleRow
          title="Искать рыночные предложения"
          description="Разрешает ручной поиск цен из drawer листинга. Автоматические периодические запуски не включаются."
          checked={settings.market_research_enabled}
          onCheckedChange={(value) => updateAndSave('market_research_enabled', value)}
          disabled={!canEdit || saving}
        />

        <ResearchGeographyPicker
          regionPreset={settings.region_preset}
          countryCodes={settings.country_codes}
          canEdit={canEdit}
          saving={saving}
          onChange={changeGeography}
        />

        <div className="max-w-xl rounded-xl border bg-card/70 p-4">
          <div className="space-y-2">
            <label htmlFor="research-ttl" className="text-sm font-medium">Срок актуальности цены, часов</label>
            <Input
              id="research-ttl"
              type="number"
              min={1}
              max={720}
              value={settings.price_ttl_hours}
              onChange={(event) => setSettings({ ...settings, price_ttl_hours: Number(event.target.value) })}
              onBlur={() => void persist(settings)}
              disabled={!canEdit || saving}
            />
            <p className="text-xs text-muted-foreground">
              Пока данные свежие, обычное обновление не расходует платный поисковый запрос.
            </p>
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <ToggleRow
            title="Маркетплейсы"
            description="Включать предложения крупных площадок вместе с интернет-магазинами."
            checked={settings.include_marketplaces}
            onCheckedChange={(value) => updateAndSave('include_marketplaces', value)}
            disabled={!canEdit || saving}
          />
          <ToggleRow
            title="Товары б/у"
            description="По умолчанию сравнение строится только по новым деталям."
            checked={settings.include_used}
            onCheckedChange={(value) => updateAndSave('include_used', value)}
            disabled={!canEdit || saving}
          />
          <ToggleRow
            title="Предложения под заказ"
            description="Показывать цены продавцов, у которых деталь доступна по предзаказу."
            checked={settings.include_preorder}
            onCheckedChange={(value) => updateAndSave('include_preorder', value)}
            disabled={!canEdit || saving}
          />
          <ToggleRow
            title="Возможные аналоги"
            description="Показывать заменители отдельно; они не попадут в статистику без проверки."
            checked={settings.include_analogues}
            onCheckedChange={(value) => updateAndSave('include_analogues', value)}
            disabled={!canEdit || saving}
          />
          <ToggleRow
            title="Только точные совпадения"
            description="Строго проверять артикул/OEM, бренд и тип детали перед сравнением цены."
            checked={settings.exact_matches_only}
            onCheckedChange={(value) => updateAndSave('exact_matches_only', value)}
            disabled={!canEdit || saving}
          />
          <div className="rounded-lg border p-3">
            <p className="text-sm font-medium">Валюта сравнения</p>
            <div className="mt-2 flex items-center gap-2">
              <Badge variant="outline">RUB · ₽</Badge>
              <span className="text-xs text-muted-foreground">Иностранные цены сохраняются в оригинале.</span>
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-3 rounded-lg bg-muted/40 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <SearchCheck className="mt-0.5 h-5 w-5 text-emerald-600" />
            <p className="text-sm text-muted-foreground">
              Сомнительные совпадения сохраняются для проверки, но не влияют на расчёт типичной, самой низкой и самой высокой цены.
            </p>
          </div>
          {canEdit && (
            <Button onClick={save} disabled={saving} className="shrink-0">
              {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {saving ? 'Сохраняем…' : savedFeedback ? 'Сохранено' : 'Сохранить сейчас'}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
