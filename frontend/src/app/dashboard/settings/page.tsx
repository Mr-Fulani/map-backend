'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { tenantApi, accountApi, notificationApi, productApi } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Switch } from '@/components/ui/switch';
import { toast } from 'sonner';
import { Loader2, Plus, Trash2, Copy, Check, ExternalLink, Bell, BellOff, KeyRound, Upload, FileSpreadsheet, Server, FileCode2, AlertCircle, CheckCircle2, Store, Search } from 'lucide-react';
import { profileApi, datasourceApi } from '@/lib/api';
import {
  CatalogCategoryPicker,
  type CatalogCategoryOption,
} from '@/components/products/catalog-category-picker';

interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
}

interface Account {
  id: number;
  name: string;
  marketplace: string;
  external_id: string;
  is_active: boolean;
  default_address: string;
  default_seller_address_id: string;
  default_manager_name: string;
  default_contact_phone: string;
  autoload_active: boolean | null;
  autoload_checked_at: string | null;
  autoload_subscription_ends_at: string | null;
  avito_status: AvitoAccountHealth | null;
  created_at: string;
}

interface AvitoAccountHealth {
  connection_status: 'unknown' | 'connected' | 'auth_error' | 'unavailable';
  autoload_status: 'unknown' | 'enabled' | 'disabled' | 'missing' | 'forbidden';
  feed_configured: boolean | null;
  profile_checked_at: string | null;
  profile_stale: boolean;
  tariff_status: 'unknown' | 'active' | 'inactive' | 'not_found' | 'unavailable';
  tariff_name: string;
  tariff_started_at: string | null;
  tariff_ends_at: string | null;
  subscription_ends_at: string | null;
  subscription_source: 'avito_tariff' | 'manual' | 'unavailable';
  days_left: number | null;
  tariff_price: string | null;
  placements_remaining: number | null;
  placements_total: number | null;
  scheduled_tariff: { name?: string; starts_at?: string | null; price?: string | null };
  tariff_checked_at: string | null;
  tariff_stale: boolean;
  last_attempted_at: string | null;
  last_error_code: string;
  last_error_message: string;
}

interface AutoloadCheck {
  activated: boolean;
  feed_url: string;
  stale?: boolean;
  status?: AvitoAccountHealth;
}

interface PlacementAddress {
  id: number;
  account: number;
  account_name: string;
  name: string;
  seller_address_id: string;
  address: string;
  manager_name: string;
  contact_phone: string;
  is_default: boolean;
  is_active: boolean;
}

interface DataSource {
  id: number;
  name: string;
  type: string;
  url?: string;
  is_active?: boolean;
}

interface NotificationSettings {
  telegram_connected: boolean;
  telegram_username: string;
  notify_email: string;
  notify_on_error: boolean;
  notify_on_critical: boolean;
}

interface CatalogCategory extends CatalogCategoryOption {
  default_image_url: string;
  default_image_source_name: string;
}

interface CatalogDomain {
  id: number;
  slug: string;
  name: string;
  short_name: string;
  is_active: boolean;
  is_enabled_for_tenant: boolean;
}

interface SourceCategory {
  source_category: string;
  catalog_category: number | null;
}

interface CatalogCategoryMapping {
  id: number;
  source_category: string;
  category: number;
  category_name: string;
  category_domain: string;
}

const SETTINGS_TABS = [
  'profile', 'organization', 'api-keys', 'marketplaces', 'datasources',
  'catalog-categories', 'pricing', 'notifications',
] as const;
type SettingsTab = typeof SETTINGS_TABS[number];

const FALLBACK_DOMAIN_LABELS: Record<string, string> = {
  auto_parts: 'Автозапчасти',
  generic: 'Разное',
  jewellery: 'Украшения',
  apparel: 'Одежда',
  unknown: 'Не определено',
};

// Раскладывает плоский список категорий в порядок дерева (родитель → дети) с
// глубиной для отступов. Категория с отсутствующим родителем считается корневой.
function buildCategoryTree(cats: CatalogCategory[]): { category: CatalogCategory; depth: number }[] {
  const ids = new Set(cats.map((c) => c.id));
  const byParent = new Map<number | null, CatalogCategory[]>();
  for (const c of cats) {
    const key = c.parent && ids.has(c.parent) ? c.parent : null;
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key)!.push(c);
  }
  byParent.forEach((list) => list.sort((a, b) => a.name.localeCompare(b.name, 'ru')));
  const result: { category: CatalogCategory; depth: number }[] = [];
  const walk = (parent: number | null, depth: number) => {
    for (const c of byParent.get(parent) ?? []) {
      result.push({ category: c, depth });
      walk(c.id, depth + 1);
    }
  };
  walk(null, 0);
  return result;
}

function MarginEditor({
  categories,
  onSaved,
}: {
  categories: CatalogCategory[];
  onSaved: () => Promise<void>;
}) {
  const [search, setSearch] = useState('');
  const [values, setValues] = useState<Record<number, string>>(() =>
    Object.fromEntries(categories.map((c) => [c.id, c.default_margin_pct ?? ''])),
  );
  const [saving, setSaving] = useState<Record<number, boolean>>({});

  useEffect(() => {
    setValues(Object.fromEntries(categories.map((c) => [c.id, c.default_margin_pct ?? ''])));
  }, [categories]);

  const handleSave = async (id: number) => {
    setSaving((prev) => ({ ...prev, [id]: true }));
    try {
      await productApi.patchCatalogCategory(id, {
        default_margin_pct: values[id] === '' ? null : values[id],
      });
      await onSaved();
      toast.success('Наценка сохранена');
    } catch {
      toast.error('Не удалось сохранить наценку');
    } finally {
      setSaving((prev) => ({ ...prev, [id]: false }));
    }
  };

  const rows = search
    ? categories
        .filter((c) => c.name.toLowerCase().includes(search.toLowerCase()))
        .map((c) => ({ category: c, depth: 0 }))
    : buildCategoryTree(categories);

  return (
    <div className="space-y-4">
      <Input
        placeholder="Поиск по категориям..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        className="max-w-sm"
      />
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {search ? 'Ничего не найдено' : 'Категории не найдены.'}
        </p>
      ) : (
        <div className="space-y-2">
          {rows.map(({ category, depth }) => (
            <div
              key={category.id}
              style={depth ? { marginLeft: depth * 20 } : undefined}
              className={`flex flex-col gap-3 rounded-lg border p-3 md:flex-row md:items-center md:justify-between${
                depth ? ' border-l-2 border-l-primary/40 bg-muted/30' : ''
              }`}
            >
              <div className="flex min-w-0 items-center gap-3">
                <div className="h-10 w-10 shrink-0 overflow-hidden rounded-md border bg-muted">
                  {category.default_image_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={category.default_image_url} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center text-[10px] text-muted-foreground">
                      Нет фото
                    </div>
                  )}
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{category.name}</p>
                  {category.root_domain_name && (
                    <p className="text-xs text-muted-foreground">{category.root_domain_name}</p>
                  )}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Input
                  value={values[category.id] ?? ''}
                  onChange={(e) => setValues((prev) => ({ ...prev, [category.id]: e.target.value }))}
                  inputMode="decimal"
                  placeholder="Наследовать"
                  className="h-8 w-24 text-sm"
                />
                <span className="text-sm text-muted-foreground">%</span>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8"
                  disabled={saving[category.id]}
                  onClick={() => handleSave(category.id)}
                >
                  {saving[category.id] ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Сохранить'}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground md:w-56 md:text-right">
                Итог: {category.effective_margin_pct}%
                {category.margin_inherited_from_name
                  ? ` · от «${category.margin_inherited_from_name}»`
                  : category.default_margin_pct == null
                    ? ' · без наценки'
                    : ' · собственная'}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const { user, tenant } = useAuth();
  const [activeTab, setActiveTab] = useState<SettingsTab>('profile');
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [catalogCategories, setCatalogCategories] = useState<CatalogCategory[]>([]);
  const [catalogDomains, setCatalogDomains] = useState<CatalogDomain[]>([]);
  const [sourceCategories, setSourceCategories] = useState<SourceCategory[]>([]);
  const [catalogMappings, setCatalogMappings] = useState<CatalogCategoryMapping[]>([]);
  const [loadingKeys, setLoadingKeys] = useState(true);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [loadingDatasources, setLoadingDatasources] = useState(true);
  const [loadingCatalogCategories, setLoadingCatalogCategories] = useState(true);
  const [newKeyName, setNewKeyName] = useState('');
  const [creatingKey, setCreatingKey] = useState(false);
  const [newKeyValue, setNewKeyValue] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokingId, setRevokingId] = useState<number | null>(null);
  const [deletingAccountId, setDeletingAccountId] = useState<number | null>(null);
  const [togglingAccountId, setTogglingAccountId] = useState<number | null>(null);
  const [editingAccountId, setEditingAccountId] = useState<number | null>(null);
  const [editAccountName, setEditAccountName] = useState('');
  const [autoloadStatus, setAutoloadStatus] = useState<Record<number, AutoloadCheck | null>>({});
  const [checkingAutoloadId, setCheckingAutoloadId] = useState<number | null>(null);
  const [copiedFeedId, setCopiedFeedId] = useState<number | null>(null);
  const [savingPlacementAccountId, setSavingPlacementAccountId] = useState<number | null>(null);
  const [savingSubscriptionAccountId, setSavingSubscriptionAccountId] = useState<number | null>(null);
  const [placementAddresses, setPlacementAddresses] = useState<PlacementAddress[]>([]);
  const [savingAddressAccountId, setSavingAddressAccountId] = useState<number | null>(null);
  const [addressDrafts, setAddressDrafts] = useState<Record<number, {
    name: string;
    seller_address_id: string;
    address: string;
    manager_name: string;
    contact_phone: string;
  }>>({});
  const [deletingDatasourceId, setDeletingDatasourceId] = useState<number | null>(null);
  const [creatingCatalogCategory, setCreatingCatalogCategory] = useState(false);
  const [uploadingCategoryImageId, setUploadingCategoryImageId] = useState<number | null>(null);
  const [savingCatalogCategoryId, setSavingCatalogCategoryId] = useState<number | null>(null);
  const [savingCatalogDomainSlug, setSavingCatalogDomainSlug] = useState<string | null>(null);
  const [editingCatalogCategoryId, setEditingCatalogCategoryId] = useState<number | null>(null);
  const [newCatalogCategoryName, setNewCatalogCategoryName] = useState('');
  const [newCatalogCategoryDomain, setNewCatalogCategoryDomain] = useState('unknown');
  const [newCatalogCategoryParent, setNewCatalogCategoryParent] = useState('');
  const [editCatalogCategoryName, setEditCatalogCategoryName] = useState('');
  const [editCatalogCategoryDomain, setEditCatalogCategoryDomain] = useState('unknown');
  const [categorySearch, setCategorySearch] = useState('');
  const [savingMappingSource, setSavingMappingSource] = useState<string | null>(null);
  
  // File upload state
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [uploadingCsv, setUploadingCsv] = useState(false);
  const [newDatasourceType, setNewDatasourceType] = useState<'1c_http' | '1c_xml'>('1c_http');
  const [newDatasourceName, setNewDatasourceName] = useState('Основной склад');
  const [newDatasourceUrl, setNewDatasourceUrl] = useState('');
  const [newDatasourceUser, setNewDatasourceUser] = useState('');
  const [newDatasourcePassword, setNewDatasourcePassword] = useState('');
  const [creatingDatasource, setCreatingDatasource] = useState(false);
  
  // Add Avito Account state
  const [showAddAccount, setShowAddAccount] = useState(false);
  const [newAccountName, setNewAccountName] = useState('');
  const [newClientId, setNewClientId] = useState('');
  const [newClientSecret, setNewClientSecret] = useState('');
  const [creatingAccount, setCreatingAccount] = useState(false);

  // Синхронизация активной вкладки с URL-хэшем
  const didMount = useRef(false);
  useEffect(() => {
    function syncTab() {
      const rawHash = window.location.hash.slice(1);
      const hash = (rawHash === 'accounts' ? 'marketplaces' : rawHash) as SettingsTab;
      if (SETTINGS_TABS.includes(hash)) setActiveTab(hash);
    }
    syncTab();
    window.addEventListener('hashchange', syncTab);
    return () => window.removeEventListener('hashchange', syncTab);
  }, []);

  // При смене вкладки обновляем хэш (кроме первого рендера)
  useEffect(() => {
    if (!didMount.current) { didMount.current = true; return; }
    window.history.replaceState(null, '', `#${activeTab}`);
  }, [activeTab]);

  // Profile state
  const [phone, setPhone] = useState('');
  const [savingPhone, setSavingPhone] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [changingPassword, setChangingPassword] = useState(false);
  const [newEmail, setNewEmail] = useState('');
  const [requestingEmail, setRequestingEmail] = useState(false);
  const [emailSent, setEmailSent] = useState(false);

  // Notifications state
  const [notifSettings, setNotifSettings] = useState<NotificationSettings | null>(null);
  const [loadingNotif, setLoadingNotif] = useState(true);
  const [connectingTg, setConnectingTg] = useState(false);
  const [disconnectingTg, setDisconnectingTg] = useState(false);
  const [testingNotif, setTestingNotif] = useState(false);
  const [savingNotif, setSavingNotif] = useState(false);
  const [notifEmail, setNotifEmail] = useState('');
  const [notifOnError, setNotifOnError] = useState(true);
  const [notifOnCritical, setNotifOnCritical] = useState(true);
  const telegramPopupRef = useRef<Window | null>(null);
  const telegramPollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (telegramPollTimerRef.current) clearTimeout(telegramPollTimerRef.current);
      telegramPopupRef.current?.close();
    };
  }, []);

  const domainLabel = (slug: string) => {
    const domain = catalogDomains.find((item) => item.slug === slug);
    return domain?.short_name || domain?.name || FALLBACK_DOMAIN_LABELS[slug] || slug;
  };
  const domainOptions = catalogDomains.length > 0
    ? catalogDomains
    : Object.entries(FALLBACK_DOMAIN_LABELS).map(([slug, name], index) => ({
        id: -index - 1,
        slug,
        name,
        short_name: '',
        is_active: true,
        is_enabled_for_tenant: true,
      }));
  const enabledDomainOptions = domainOptions.filter((domain) => domain.is_enabled_for_tenant);

  const loadDatasources = useCallback(async () => {
    const r = await datasourceApi.list();
    setDatasources(r.data.data ?? r.data);
  }, []);

  // Подставляем телефон из данных пользователя при загрузке
  useEffect(() => {
    if (user && 'phone' in user) {
      setPhone((user as { phone?: string }).phone ?? '');
    }
  }, [user]);

  useEffect(() => {
    if (enabledDomainOptions.length === 0) return;
    if (!enabledDomainOptions.some((domain) => domain.slug === newCatalogCategoryDomain)) {
      setNewCatalogCategoryDomain(enabledDomainOptions[0].slug);
    }
  }, [enabledDomainOptions, newCatalogCategoryDomain]);

  useEffect(() => {
    tenantApi.getApiKeys()
      .then((r) => setApiKeys(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingKeys(false));

    accountApi.list()
      .then((r) => {
        const list: Account[] = r.data.data ?? r.data;
        setAccounts(list);
        // Подставляем последний известный статус Автозагрузки, чтобы плашка
        // не сбрасывалась в «неизвестно» при каждой перезагрузке страницы.
        setAutoloadStatus((prev) => {
          const seeded = { ...prev };
          for (const acc of list) {
            if (acc.avito_status || (acc.autoload_active !== null && acc.autoload_active !== undefined)) {
              seeded[acc.id] = {
                activated: acc.avito_status
                  ? acc.avito_status.autoload_status === 'enabled'
                  : Boolean(acc.autoload_active),
                feed_url: '',
                stale: acc.avito_status?.profile_stale,
                status: acc.avito_status ?? undefined,
              };
            }
          }
          return seeded;
        });
      })
      .catch(() => {})
      .finally(() => setLoadingAccounts(false));

    loadPlacementAddresses();

    loadDatasources()
      .catch(() => {})
      .finally(() => setLoadingDatasources(false));

    tenantApi.catalogDomains()
      .then((r) => setCatalogDomains(r.data.data ?? r.data))
      .catch(() => setCatalogDomains([]));

    loadCatalogCategories();

    notificationApi.getSettings()
      .then((r) => {
        const d = r.data.data as NotificationSettings;
        setNotifSettings(d);
        setNotifEmail(d.notify_email);
        setNotifOnError(d.notify_on_error);
        setNotifOnCritical(d.notify_on_critical);
      })
      .catch(() => {})
      .finally(() => setLoadingNotif(false));
  }, [loadDatasources]);

  async function loadCatalogCategories() {
    setLoadingCatalogCategories(true);
    try {
      const [categoriesRes, sourceRes, mappingsRes] = await Promise.all([
        productApi.catalogCategories(),
        productApi.catalogSourceCategories(),
        productApi.catalogCategoryMappings(),
      ]);
      const cats: CatalogCategory[] = categoriesRes.data.data ?? categoriesRes.data;
      cats.sort((a, b) => a.name.localeCompare(b.name, 'ru'));
      setCatalogCategories(cats);
      setSourceCategories(sourceRes.data.data ?? sourceRes.data);
      setCatalogMappings(mappingsRes.data.data ?? mappingsRes.data);
    } catch {
      toast.error('Не удалось загрузить категории каталога');
    } finally {
      setLoadingCatalogCategories(false);
    }
  }

  async function loadPlacementAddresses() {
    try {
      const res = await accountApi.listPlacementAddresses();
      setPlacementAddresses(res.data.data ?? res.data);
    } catch {
      setPlacementAddresses([]);
    }
  }

  async function savePhone() {
    setSavingPhone(true);
    try {
      await profileApi.updatePhone(phone);
      toast.success('Телефон сохранён');
    } catch {
      toast.error('Ошибка сохранения');
    } finally {
      setSavingPhone(false);
    }
  }

  async function changePassword() {
    if (newPassword !== confirmPassword) {
      toast.error('Пароли не совпадают');
      return;
    }
    setChangingPassword(true);
    try {
      await profileApi.changePassword(currentPassword, newPassword);
      toast.success('Пароль изменён');
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { message?: string } } })?.response?.data?.message;
      toast.error(msg ?? 'Ошибка смены пароля');
    } finally {
      setChangingPassword(false);
    }
  }

  async function requestEmailChange() {
    setRequestingEmail(true);
    try {
      await profileApi.changeEmail(newEmail);
      setEmailSent(true);
      toast.success('Письмо отправлено');
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { message?: string } } })?.response?.data?.message;
      toast.error(msg ?? 'Ошибка отправки письма');
    } finally {
      setRequestingEmail(false);
    }
  }

  async function createKey() {
    if (!newKeyName.trim()) return;
    setCreatingKey(true);
    try {
      const res = await tenantApi.createApiKey(newKeyName.trim());
      const body = res.data.data ?? res.data;
      setNewKeyValue(body.key);
      setApiKeys((prev) => [...prev, body]);
      setNewKeyName('');
    } catch {
      toast.error('Не удалось создать ключ');
    } finally {
      setCreatingKey(false);
    }
  }

  async function revokeKey(id: number) {
    setRevokingId(id);
    try {
      await tenantApi.revokeApiKey(id);
      setApiKeys((prev) => prev.filter((k) => k.id !== id));
      toast.success('Ключ отозван');
    } catch {
      toast.error('Ошибка при отзыве ключа');
    } finally {
      setRevokingId(null);
    }
  }

  async function createAccount(e: React.FormEvent) {
    e.preventDefault();
    setCreatingAccount(true);
    try {
      const { data: result } = await accountApi.create({
        marketplace: 'avito',
        name: newAccountName || 'Новый аккаунт',
        external_id: newClientId,
        client_id: newClientId,
        client_secret: newClientSecret,
      });
      toast.success('Аккаунт добавлен');
      setAccounts((prev) => [...prev, result.data ?? result]);
      setShowAddAccount(false);
      setNewAccountName('');
      setNewClientId('');
      setNewClientSecret('');
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status?: number; data?: Record<string, unknown> } };
      if (axiosErr?.response?.status === 409) {
        const message = String(axiosErr.response.data?.detail || 'Этот аккаунт Avito уже подключён');
        toast.error(message);
        return;
      }
      const errorData = axiosErr?.response?.data;
      const message = String(
        errorData?.message ||
        errorData?.detail ||
        errorData?.errors ||
        'Ошибка при добавлении аккаунта',
      );
      toast.error(message);
    } finally {
      setCreatingAccount(false);
    }
  }

  async function toggleAccount(id: number, isActive: boolean) {
    setTogglingAccountId(id);
    try {
      const res = await accountApi.patch(id, { is_active: isActive });
      setAccounts((prev) => prev.map((a) => a.id === id ? (res.data.data ?? res.data) : a));
    } catch {
      toast.error('Не удалось изменить статус');
    } finally {
      setTogglingAccountId(null);
    }
  }

  async function saveAccountName(id: number) {
    if (!editAccountName.trim()) return;
    try {
      const res = await accountApi.patch(id, { name: editAccountName.trim() });
      setAccounts((prev) => prev.map((a) => a.id === id ? (res.data.data ?? res.data) : a));
      setEditingAccountId(null);
      toast.success('Название сохранено');
    } catch {
      toast.error('Не удалось сохранить название');
    }
  }

  function updateAccountDraft(id: number, data: Partial<Account>) {
    setAccounts((prev) => prev.map((a) => a.id === id ? { ...a, ...data } : a));
  }

  async function saveAccountPlacement(account: Account) {
    setSavingPlacementAccountId(account.id);
    try {
      const res = await accountApi.patch(account.id, {
        default_address: account.default_address || '',
        default_seller_address_id: account.default_seller_address_id || '',
        default_manager_name: account.default_manager_name || '',
        default_contact_phone: account.default_contact_phone || '',
      });
      setAccounts((prev) => prev.map((a) => a.id === account.id ? (res.data.data ?? res.data) : a));
      toast.success('Настройки размещения сохранены');
    } catch {
      toast.error('Не удалось сохранить настройки размещения');
    } finally {
      setSavingPlacementAccountId(null);
    }
  }

  async function saveAutoloadSubscriptionEnd(account: Account) {
    setSavingSubscriptionAccountId(account.id);
    try {
      const res = await accountApi.patch(account.id, {
        autoload_subscription_ends_at: account.autoload_subscription_ends_at || null,
      });
      const updated: Account = res.data.data ?? res.data;
      setAccounts((prev) => prev.map((item) => item.id === account.id ? updated : item));
      setAutoloadStatus((prev) => ({
        ...prev,
        [account.id]: {
          activated: updated.avito_status?.autoload_status === 'enabled',
          feed_url: prev[account.id]?.feed_url || '',
          stale: updated.avito_status?.profile_stale,
          status: updated.avito_status ?? undefined,
        },
      }));
      toast.success(
        updated.autoload_subscription_ends_at
          ? 'Дата окончания Автозагрузки сохранена'
          : 'Ручная дата удалена',
      );
    } catch {
      toast.error('Не удалось сохранить дату окончания Автозагрузки');
    } finally {
      setSavingSubscriptionAccountId(null);
    }
  }

  function updateAddressDraft(accountId: number, data: Partial<Record<'name' | 'seller_address_id' | 'address' | 'manager_name' | 'contact_phone', string>>) {
    const emptyDraft = {
      name: '',
      seller_address_id: '',
      address: '',
      manager_name: '',
      contact_phone: '',
    };
    setAddressDrafts((prev) => ({
      ...prev,
      [accountId]: { ...emptyDraft, ...(prev[accountId] || {}), ...data },
    }));
  }

  async function createPlacementAddress(accountId: number) {
    const draft = addressDrafts[accountId];
    if (!draft?.name?.trim()) {
      toast.error('Укажите название адреса');
      return;
    }
    setSavingAddressAccountId(accountId);
    try {
      const res = await accountApi.createPlacementAddress({
        account: accountId,
        name: draft.name.trim(),
        seller_address_id: draft.seller_address_id || '',
        address: draft.address || '',
        manager_name: draft.manager_name || '',
        contact_phone: draft.contact_phone || '',
        is_default: placementAddresses.filter((item) => item.account === accountId && item.is_active).length === 0,
        is_active: true,
      });
      setPlacementAddresses((prev) => [...prev, res.data.data ?? res.data]);
      setAddressDrafts((prev) => ({ ...prev, [accountId]: {
        name: '',
        seller_address_id: '',
        address: '',
        manager_name: '',
        contact_phone: '',
      } }));
      toast.success('Адрес добавлен');
    } catch {
      toast.error('Не удалось добавить адрес');
    } finally {
      setSavingAddressAccountId(null);
    }
  }

  async function setDefaultPlacementAddress(address: PlacementAddress) {
    try {
      const res = await accountApi.patchPlacementAddress(address.id, { is_default: true });
      const updated = res.data.data ?? res.data;
      setPlacementAddresses((prev) => prev.map((item) => {
        if (item.account === address.account) return item.id === address.id ? updated : { ...item, is_default: false };
        return item;
      }));
    } catch {
      toast.error('Не удалось назначить адрес по умолчанию');
    }
  }

  async function deletePlacementAddress(id: number) {
    try {
      await accountApi.deletePlacementAddress(id);
      setPlacementAddresses((prev) => prev.filter((item) => item.id !== id));
      toast.success('Адрес удалён');
    } catch {
      toast.error('Не удалось удалить адрес');
    }
  }

  async function deleteAccount(id: number) {
    setDeletingAccountId(id);
    try {
      await accountApi.delete(id);
      setAccounts((prev) => prev.filter((a) => a.id !== id));
      toast.success('Аккаунт удалён');
    } catch {
      toast.error('Ошибка при удалении аккаунта');
    } finally {
      setDeletingAccountId(null);
    }
  }

  async function checkAutoloadStatus(id: number) {
    setCheckingAutoloadId(id);
    try {
      const { data } = await accountApi.checkAutoload(id);
      setAutoloadStatus((prev) => ({ ...prev, [id]: data }));
      if (data.stale) toast.warning('Avito временно недоступен — показаны последние данные');
      else if (data.activated) toast.success('Данные Avito обновлены');
      else toast.error('Автозагрузка не активирована');
    } catch {
      toast.error('Не удалось проверить статус');
    } finally {
      setCheckingAutoloadId(null);
    }
  }

  async function copyFeedUrl(id: number, url: string) {
    await navigator.clipboard.writeText(url);
    setCopiedFeedId(id);
    setTimeout(() => setCopiedFeedId(null), 2000);
  }

  async function deleteDatasource(id: number) {
    setDeletingDatasourceId(id);
    try {
      await datasourceApi.delete(id);
      setDatasources((prev) => prev.filter((d) => d.id !== id));
      toast.success('Источник данных удалён');
    } catch {
      toast.error('Ошибка при удалении источника данных');
    } finally {
      setDeletingDatasourceId(null);
    }
  }

  async function createCatalogCategory(e: React.FormEvent) {
    e.preventDefault();
    if (!newCatalogCategoryName.trim()) return;
    const rootDomain = enabledDomainOptions.find((domain) => domain.slug === newCatalogCategoryDomain);
    if (!rootDomain) {
      toast.error('Сначала включите корневую категорию');
      return;
    }
    setCreatingCatalogCategory(true);
    try {
      // Если выбран родитель — создаём подкатегорию (root_domain выведет бэкенд),
      // иначе — корневую категорию в выбранном домене.
      const payload: Record<string, unknown> = newCatalogCategoryParent
        ? { name: newCatalogCategoryName.trim(), parent: Number(newCatalogCategoryParent), aliases: [], is_active: true }
        : {
            name: newCatalogCategoryName.trim(),
            root_domain: rootDomain.id,
            domain: rootDomain.slug,
            aliases: [],
            is_active: true,
          };
      await productApi.createCatalogCategory(payload);
      setNewCatalogCategoryName('');
      setNewCatalogCategoryParent('');
      setNewCatalogCategoryDomain(enabledDomainOptions[0]?.slug ?? '');
      await loadCatalogCategories();
      toast.success(newCatalogCategoryParent ? 'Подкатегория создана' : 'Категория создана');
    } catch {
      toast.error('Не удалось создать категорию');
    } finally {
      setCreatingCatalogCategory(false);
    }
  }

  function startCatalogCategoryEdit(category: CatalogCategory) {
    setEditingCatalogCategoryId(category.id);
    setEditCatalogCategoryName(category.name);
    setEditCatalogCategoryDomain(category.root_domain_slug || category.domain);
  }

  async function updateCatalogCategory(category: CatalogCategory, data: Partial<CatalogCategory>) {
    const rootSlug = data.root_domain_slug ?? category.root_domain_slug ?? data.domain ?? category.domain;
    const rootDomain = domainOptions.find((domain) => domain.slug === rootSlug);
    setSavingCatalogCategoryId(category.id);
    try {
      await productApi.updateCatalogCategory(category.id, {
        name: data.name ?? category.name,
        root_domain: rootDomain?.id ?? category.root_domain,
        domain: rootDomain?.slug ?? data.domain ?? category.domain,
        aliases: category.aliases ?? [],
        is_active: data.is_active ?? category.is_active,
      });
      await loadCatalogCategories();
      setEditingCatalogCategoryId(null);
      toast.success('Категория сохранена');
    } catch {
      toast.error('Не удалось сохранить категорию');
    } finally {
      setSavingCatalogCategoryId(null);
    }
  }

  async function saveCatalogCategoryEdit(category: CatalogCategory) {
    if (!editCatalogCategoryName.trim()) return;
    await updateCatalogCategory(category, {
      name: editCatalogCategoryName.trim(),
      root_domain_slug: editCatalogCategoryDomain,
    });
  }

  async function toggleCatalogCategory(category: CatalogCategory) {
    setSavingCatalogCategoryId(category.id);
    try {
      // Тоггл действует на всю ветку: авто-классификация рассматривает только
      // активные категории, поэтому отключённые подкатегории тоже должны гаснуть.
      const res = await productApi.toggleCatalogCategoryBranch(category.id, !category.is_active);
      const data = res.data.data ?? {};
      const affected = data.affected_categories ?? 1;
      if (category.is_active) {
        toast.success(
          affected > 1
            ? `Отключена ветка: ${affected} категорий${data.affected_products ? `, товаров на переклассификацию: ${data.affected_products}` : ''}`
            : 'Категория отключена',
        );
      } else {
        toast.success(affected > 1 ? `Включена ветка: ${affected} категорий` : 'Категория включена');
      }
      await loadCatalogCategories();
    } catch {
      toast.error('Не удалось изменить статус категории');
    } finally {
      setSavingCatalogCategoryId(null);
    }
  }

  async function removeCatalogCategory(category: CatalogCategory) {
    if (!window.confirm(`Удалить категорию «${category.name}» без возможности восстановления?`)) return;
    setSavingCatalogCategoryId(category.id);
    try {
      await productApi.deleteCatalogCategory(category.id, true);
      await loadCatalogCategories();
      toast.success('Категория удалена');
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status?: number; data?: { message?: string } } };
      const message = axiosErr?.response?.status === 409
        ? (axiosErr.response.data?.message || 'Категорию нельзя удалить: есть привязки.')
        : 'Не удалось удалить категорию';
      toast.error(message);
    } finally {
      setSavingCatalogCategoryId(null);
    }
  }

  async function setCatalogDomainEnabled(domainSlug: string, isEnabled: boolean) {
    setSavingCatalogDomainSlug(domainSlug);
    try {
      await tenantApi.setCatalogDomainEnabled(domainSlug, isEnabled);
      const res = await tenantApi.catalogDomains();
      setCatalogDomains(res.data.data ?? res.data);
      await loadCatalogCategories();
      toast.success(isEnabled ? 'Корневая категория включена' : 'Корневая категория отключена');
    } catch {
      toast.error('Не удалось изменить корневую категорию');
    } finally {
      setSavingCatalogDomainSlug(null);
    }
  }

  async function saveCatalogCategoryMapping(sourceCategory: string, categoryId: string) {
    const currentMapping = catalogMappings.find((item) => item.source_category === sourceCategory);
    setSavingMappingSource(sourceCategory);
    try {
      if (!categoryId) {
        if (currentMapping) {
          await productApi.deleteCatalogCategoryMapping(currentMapping.id);
          await loadCatalogCategories();
          toast.success('Привязка удалена');
        }
        return;
      }
      await productApi.createCatalogCategoryMapping({
        source_category: sourceCategory,
        category: Number(categoryId),
      });
      await loadCatalogCategories();
      toast.success('Привязка сохранена');
    } catch {
      toast.error('Не удалось сохранить привязку');
    } finally {
      setSavingMappingSource(null);
    }
  }

  async function uploadCatalogCategoryImage(categoryId: number, file: File | null) {
    if (!file) return;
    setUploadingCategoryImageId(categoryId);
    try {
      await productApi.uploadCatalogCategoryImage(categoryId, file);
      await loadCatalogCategories();
      toast.success('Картинка категории сохранена');
    } catch {
      toast.error('Не удалось загрузить картинку категории');
    } finally {
      setUploadingCategoryImageId(null);
    }
  }

  async function deleteCatalogCategoryImage(categoryId: number) {
    setUploadingCategoryImageId(categoryId);
    try {
      await productApi.deleteCatalogCategoryImage(categoryId);
      await loadCatalogCategories();
      toast.success('Картинка категории удалена');
    } catch {
      toast.error('Не удалось удалить картинку категории');
    } finally {
      setUploadingCategoryImageId(null);
    }
  }

  async function uploadCsvFile(file: File, confirm: boolean) {
    try {
      const { data: uploadResult } = await datasourceApi.uploadCsv(file, confirm);
      toast.success(`Файл загружен: ${uploadResult.data?.rows_count || uploadResult.items?.length || 0} строк`);
      setCsvFile(null);
      await loadDatasources();
    } catch (err: unknown) {
      const axiosErr = err as {
        response?: { status?: number; data?: { status?: string; message?: string; detail?: string } };
      };
      // Бэкенд нашёл такой же файл (по содержимому или имени) — спрашиваем подтверждение.
      if (axiosErr?.response?.status === 409 && axiosErr.response.data?.status === 'duplicate') {
        const msg = axiosErr.response.data.message || 'Такой файл уже загружали. Загрузить повторно?';
        if (window.confirm(msg)) {
          await uploadCsvFile(file, true);
        }
        return;
      }
      const message = axiosErr?.response?.data?.detail || axiosErr?.response?.data?.message || 'Ошибка загрузки файла';
      toast.error(message);
    }
  }

  async function handleUploadCsv(e: React.FormEvent) {
    e.preventDefault();
    if (!csvFile) return;
    setUploadingCsv(true);
    try {
      await uploadCsvFile(csvFile, false);
    } finally {
      setUploadingCsv(false);
    }
  }

  async function handleCreateDatasource(e: React.FormEvent) {
    e.preventDefault();
    setCreatingDatasource(true);
    try {
      await datasourceApi.create({
        name: newDatasourceName,
        type: newDatasourceType,
        credentials: {
          url: newDatasourceUrl,
          user: newDatasourceUser,
          password: newDatasourcePassword,
        },
      });
      toast.success('Источник данных добавлен');
      setNewDatasourceName('Основной склад');
      setNewDatasourceUrl('');
      setNewDatasourceUser('');
      setNewDatasourcePassword('');
      await loadDatasources();
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string; message?: string } } };
      const message = axiosErr?.response?.data?.detail || axiosErr?.response?.data?.message || 'Не удалось добавить источник';
      toast.error(message);
    } finally {
      setCreatingDatasource(false);
    }
  }

  async function connectTelegram() {
    if (telegramPollTimerRef.current) clearTimeout(telegramPollTimerRef.current);
    telegramPopupRef.current?.close();

    const width = 520;
    const height = 720;
    const left = Math.max(0, window.screenX + (window.outerWidth - width) / 2);
    const top = Math.max(0, window.screenY + (window.outerHeight - height) / 2);
    const popup = window.open(
      'about:blank',
      'map-telegram-connect',
      `popup=yes,width=${width},height=${height},left=${left},top=${top}`,
    );

    if (!popup) {
      toast.error('Браузер заблокировал окно Telegram. Разрешите всплывающие окна для этого сайта.');
      return;
    }

    telegramPopupRef.current = popup;
    popup.document.title = 'Подключение Telegram';
    popup.document.body.textContent = 'Открываем Telegram…';
    setConnectingTg(true);
    try {
      const res = await notificationApi.telegramConnect();
      const botUrl = res.data.data?.bot_url as string;
      popup.location.replace(botUrl);
      toast.info('В открывшемся окне нажмите START. После привязки оно закроется автоматически.');

      let attemptsLeft = 450;
      const pollConnection = async () => {
        if (popup.closed) {
          telegramPopupRef.current = null;
          setConnectingTg(false);
          return;
        }

        try {
          const settingsRes = await notificationApi.getSettings();
          const data = settingsRes.data.data as NotificationSettings;
          if (data.telegram_connected) {
            setNotifSettings(data);
            popup.close();
            telegramPopupRef.current = null;
            setConnectingTg(false);
            toast.success('Telegram успешно подключён');
            return;
          }
        } catch {
          // Временная ошибка API не должна прерывать ожидание привязки.
        }

        attemptsLeft -= 1;
        if (attemptsLeft <= 0) {
          popup.close();
          telegramPopupRef.current = null;
          setConnectingTg(false);
          toast.error('Время привязки Telegram истекло. Повторите попытку.');
          return;
        }

        telegramPollTimerRef.current = setTimeout(pollConnection, 2000);
      };

      telegramPollTimerRef.current = setTimeout(pollConnection, 2000);
    } catch {
      popup.close();
      telegramPopupRef.current = null;
      setConnectingTg(false);
      toast.error('Не удалось создать ссылку. Проверьте настройки бота на сервере.');
    }
  }

  async function disconnectTelegram() {
    setDisconnectingTg(true);
    try {
      const res = await notificationApi.telegramDisconnect();
      setNotifSettings(res.data.data as NotificationSettings);
      toast.success('Telegram отвязан');
    } catch {
      toast.error('Ошибка при отвязке Telegram');
    } finally {
      setDisconnectingTg(false);
    }
  }

  async function testNotification() {
    setTestingNotif(true);
    try {
      await notificationApi.test();
      toast.success('Тестовое сообщение отправлено в Telegram');
    } catch {
      toast.error('Не удалось отправить. Проверьте подключение Telegram.');
    } finally {
      setTestingNotif(false);
    }
  }

  async function saveNotifSettings(overrides?: Partial<{ notify_on_error: boolean; notify_on_critical: boolean }>) {
    setSavingNotif(true);
    try {
      const res = await notificationApi.updateSettings({
        notify_email: notifEmail,
        notify_on_error: overrides?.notify_on_error ?? notifOnError,
        notify_on_critical: overrides?.notify_on_critical ?? notifOnCritical,
      });
      setNotifSettings(res.data.data as NotificationSettings);
      toast.success(overrides !== undefined ? 'Настройки сохранены' : 'Email сохранён');
    } catch {
      toast.error('Ошибка сохранения');
    } finally {
      setSavingNotif(false);
    }
  }

  async function toggleOnError(value: boolean) {
    setNotifOnError(value);
    await saveNotifSettings({ notify_on_error: value, notify_on_critical: notifOnCritical });
  }

  async function toggleOnCritical(value: boolean) {
    setNotifOnCritical(value);
    await saveNotifSettings({ notify_on_error: notifOnError, notify_on_critical: value });
  }

  function copyKey() {
    if (!newKeyValue) return;
    navigator.clipboard.writeText(newKeyValue);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="space-y-6">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight">Настройки</h1>
        <p className="text-muted-foreground">Управление организацией и интеграциями</p>
      </div>

      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as SettingsTab)}>
        <div className="overflow-x-auto pb-1">
          <TabsList className="w-full sm:w-auto">
            <TabsTrigger value="profile">Профиль</TabsTrigger>
            <TabsTrigger value="organization">Организация</TabsTrigger>
            <TabsTrigger value="api-keys">API-ключи</TabsTrigger>
            <TabsTrigger value="marketplaces">Маркетплейсы</TabsTrigger>
            <TabsTrigger value="datasources">Источники данных</TabsTrigger>
            <TabsTrigger value="catalog-categories">Категории</TabsTrigger>
            <TabsTrigger value="pricing">Наценки</TabsTrigger>
            <TabsTrigger value="notifications">Уведомления</TabsTrigger>
          </TabsList>
        </div>

        {/* Профиль */}
        <TabsContent value="profile" className="mt-4 space-y-4">
          {/* Личные данные */}
          <Card>
            <CardHeader>
              <CardTitle>Личные данные</CardTitle>
              <CardDescription>Email — только через подтверждение. Телефон меняется сразу.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label>Email</Label>
                  <Input value={user?.email ?? ''} readOnly className="bg-muted" />
                </div>
                <div className="space-y-2">
                  <Label>Телефон</Label>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Input
                      placeholder="+7 999 000-00-00"
                      value={phone}
                      onChange={(e) => setPhone(e.target.value)}
                    />
                    <Button onClick={savePhone} disabled={savingPhone}>
                      {savingPhone ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Сохранить'}
                    </Button>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Смена email */}
          <Card>
            <CardHeader>
              <CardTitle>Изменить email</CardTitle>
              <CardDescription>
                Письмо с подтверждением придёт на новый адрес. Ссылка действительна 24 часа.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {emailSent ? (
                <div className="rounded-lg border border-green-500/30 bg-green-500/5 p-4">
                  <p className="text-sm font-medium text-green-700">Письмо отправлено на {newEmail}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Перейдите по ссылке в письме для подтверждения. После подтверждения войдите заново.
                  </p>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="mt-2"
                    onClick={() => { setEmailSent(false); setNewEmail(''); }}
                  >
                    Отправить на другой адрес
                  </Button>
                </div>
              ) : (
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input
                    type="email"
                    placeholder="new@example.com"
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && requestEmailChange()}
                  />
                  <Button
                    onClick={requestEmailChange}
                    disabled={requestingEmail || !newEmail.trim()}
                  >
                    {requestingEmail ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Отправить'}
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Смена пароля */}
          <Card>
            <CardHeader>
              <CardTitle>Изменить пароль</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                <div className="space-y-2">
                  <Label>Текущий пароль</Label>
                  <PasswordInput
                    value={currentPassword}
                    onChange={(e) => setCurrentPassword(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label>Новый пароль</Label>
                  <PasswordInput
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="Минимум 8 символов"
                  />
                </div>
                <div className="space-y-2">
                  <Label>Подтвердите новый пароль</Label>
                  <PasswordInput
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                  />
                </div>
              </div>
              <Button
                onClick={changePassword}
                disabled={changingPassword || !currentPassword || !newPassword || !confirmPassword}
              >
                {changingPassword && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                Изменить пароль
              </Button>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Организация */}
        <TabsContent value="organization" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Информация об организации</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label>Название</Label>
                  <Input value={tenant?.name ?? ''} readOnly className="bg-muted" />
                </div>
                <div className="space-y-2">
                  <Label>Slug</Label>
                  <Input value={tenant?.slug ?? ''} readOnly className="bg-muted font-mono" />
                </div>
                <div className="space-y-2">
                  <Label>Email владельца</Label>
                  <Input value={user?.email ?? ''} readOnly className="bg-muted" />
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* API ключи */}
        <TabsContent value="api-keys" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>API-ключи</CardTitle>
              <CardDescription className="space-y-1">
                <span className="block">
                  Позволяют внешним системам обращаться к MAP API без входа в аккаунт —
                  из скриптов, CI/CD, Postman или других сервисов.
                </span>
                <span className="block text-amber-600 dark:text-amber-400">
                  Полное значение ключа показывается <strong>только один раз</strong> при создании — сохраните его сразу.
                </span>
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Новый ключ после создания */}
              {newKeyValue && (
                <div className="rounded-lg border border-green-500/30 bg-green-500/5 p-4">
                  <p className="mb-2 text-sm font-medium text-green-600">
                    Новый ключ создан — скопируйте сейчас, он больше не будет показан:
                  </p>
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                    <code className="flex-1 rounded bg-muted px-3 py-2 font-mono text-xs break-all">
                      {newKeyValue}
                    </code>
                    <Button size="sm" variant="outline" onClick={copyKey}>
                      {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                    </Button>
                  </div>
                </div>
              )}

              {/* Форма создания */}
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  placeholder="Название ключа (например: CI/CD)"
                  value={newKeyName}
                  onChange={(e) => setNewKeyName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && createKey()}
                />
                <Button onClick={createKey} disabled={creatingKey || !newKeyName.trim()}>
                  {creatingKey ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                </Button>
              </div>

              <Separator />

              {/* Список ключей */}
              {loadingKeys ? (
                <div className="space-y-2">
                  {[1, 2].map((i) => <Skeleton key={i} className="h-12 w-full" />)}
                </div>
              ) : apiKeys.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">Ключей нет</p>
              ) : (
                <div className="space-y-2">
                  {apiKeys.map((key) => (
                    <div key={key.id} className="flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="flex items-start gap-3 min-w-0">
                        <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        <div className="min-w-0">
                          <p className="break-words text-sm font-medium">{key.name}</p>
                          <code className="mt-0.5 inline-block max-w-full break-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                            {key.prefix}••••••••••••••••••••
                          </code>
                          <p className="text-xs text-muted-foreground mt-1">
                            Создан {new Date(key.created_at).toLocaleDateString('ru-RU')}
                            {key.last_used_at
                              ? ` · использован ${new Date(key.last_used_at).toLocaleDateString('ru-RU')}`
                              : ' · ещё не использован'}
                          </p>
                        </div>
                      </div>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-destructive hover:text-destructive"
                        onClick={() => revokeKey(key.id)}
                        disabled={revokingId === key.id}
                      >
                        {revokingId === key.id
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <Trash2 className="h-4 w-4" />
                        }
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Маркетплейсы */}
        <TabsContent value="marketplaces" className="mt-4 space-y-4">
          {showAddAccount && (
            <Card>
              <CardHeader>
                <CardTitle>Добавить аккаунт Avito</CardTitle>
                <CardDescription>
                  Введите данные из кабинета разработчика Avito
                </CardDescription>
              </CardHeader>
              <CardContent>
                <form onSubmit={createAccount} className="space-y-4">
                  {/* Предупреждение об Автозагрузке */}
                  <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-sm space-y-1">
                    <div className="flex items-start gap-2">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                      <div>
                        <p className="font-medium text-amber-600">Требуется Avito Автозагрузка</p>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          Для публикации объявлений через API нужен активный тариф Avito с Автозагрузкой.
                          Активируйте её в{' '}
                          <a
                            href="https://www.avito.ru/autoload/settings"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-0.5 text-primary hover:underline"
                          >
                            кабинете Avito
                            <ExternalLink className="h-3 w-3" />
                          </a>
                          {' '}до или после подключения аккаунта.
                        </p>
                      </div>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <Label>Название аккаунта</Label>
                    <Input 
                      placeholder="Например: Основной магазин" 
                      value={newAccountName}
                      onChange={(e) => setNewAccountName(e.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Client ID</Label>
                    <Input 
                      placeholder="Client ID" 
                      value={newClientId}
                      onChange={(e) => setNewClientId(e.target.value)}
                      required
                      className="font-mono"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Client Secret</Label>
                    <PasswordInput
                      placeholder="Client Secret" 
                      value={newClientSecret}
                      onChange={(e) => setNewClientSecret(e.target.value)}
                      required
                      className="font-mono"
                    />
                  </div>
                  <div className="flex flex-col justify-end gap-2 sm:flex-row">
                    <Button type="button" variant="outline" onClick={() => setShowAddAccount(false)}>
                      Отмена
                    </Button>
                    <Button type="submit" disabled={creatingAccount || !newClientId || !newClientSecret}>
                      {creatingAccount ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                      Добавить
                    </Button>
                  </div>
                </form>
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <CardTitle>Avito</CardTitle>
                  <Badge variant="default">Доступно</Badge>
                </div>
                <CardDescription>Аккаунты, Автозагрузка и публикация объявлений на Avito</CardDescription>
              </div>
              {!showAddAccount && (
                <Button onClick={() => setShowAddAccount(true)} size="sm" className="w-full sm:w-auto">
                  <Plus className="mr-2 h-4 w-4" />
                  Добавить
                </Button>
              )}
            </CardHeader>
            <CardContent>
              {loadingAccounts ? (
                <div className="space-y-2">
                  {[1, 2].map((i) => <Skeleton key={i} className="h-16 w-full" />)}
                </div>
              ) : accounts.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  Аккаунтов нет. Нажмите «Добавить», чтобы подключить Avito-аккаунт.
                </p>
              ) : (
                <div className="space-y-3">
                  {accounts.map((acc) => {
                    const al = autoloadStatus[acc.id];
                    const health = al?.status ?? acc.avito_status;
                    const accAddresses = placementAddresses.filter((item) => item.account === acc.id && item.is_active);
                    const addressDraft = addressDrafts[acc.id] || {
                      name: '',
                      seller_address_id: '',
                      address: '',
                      manager_name: '',
                      contact_phone: '',
                    };
                    return (
                    <div key={acc.id} className="rounded-lg border p-3 space-y-3 sm:p-4">
                      {/* Название + переключатель */}
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                        <div className="flex-1 min-w-0">
                          {editingAccountId === acc.id ? (
                            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                              <Input
                                value={editAccountName}
                                onChange={(e) => setEditAccountName(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') saveAccountName(acc.id);
                                  if (e.key === 'Escape') setEditingAccountId(null);
                                }}
                                className="h-7 text-sm"
                                autoFocus
                              />
                              <Button size="sm" className="h-7 px-2" onClick={() => saveAccountName(acc.id)}>
                                <Check className="h-3 w-3" />
                              </Button>
                              <Button size="sm" variant="ghost" className="h-7 px-2" onClick={() => setEditingAccountId(null)}>
                                ✕
                              </Button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              className="break-words text-left font-medium hover:underline"
                              onClick={() => { setEditingAccountId(acc.id); setEditAccountName(acc.name); }}
                            >
                              {acc.name}
                            </button>
                          )}
                          <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
                            {acc.marketplace} · ID: {acc.external_id}
                          </p>
                        </div>
                        <div className="flex shrink-0 flex-wrap items-center gap-2">
                          <div className="flex items-center gap-2">
                            <Switch
                              checked={acc.is_active}
                              disabled={togglingAccountId === acc.id}
                              onCheckedChange={(v) => toggleAccount(acc.id, v)}
                            />
                            <span className="text-sm text-muted-foreground whitespace-nowrap">
                              {togglingAccountId === acc.id
                                ? <Loader2 className="h-3 w-3 animate-spin inline" />
                                : acc.is_active ? 'Активен' : 'Неактивен'}
                            </span>
                          </div>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-destructive hover:text-destructive"
                            onClick={() => deleteAccount(acc.id)}
                            disabled={deletingAccountId === acc.id}
                          >
                            {deletingAccountId === acc.id
                              ? <Loader2 className="h-4 w-4 animate-spin" />
                              : <Trash2 className="h-4 w-4" />
                            }
                          </Button>
                        </div>
                      </div>

                      {/* Статус Avito Автозагрузки */}
                      {al?.activated ? (
                        <div className="rounded-md border border-green-500/20 bg-green-500/5 px-3 py-2 text-sm">
                          <div className="flex items-center gap-2">
                            <CheckCircle2 className="h-4 w-4 shrink-0 text-green-500" />
                            <span className="font-medium text-green-600">Автозагрузка активирована</span>
                          </div>
                          {health?.feed_configured === false && (
                            <p className="mt-1 pl-6 text-xs text-amber-600">
                              Фид MAP не найден в профиле Автозагрузки.
                            </p>
                          )}
                        </div>
                      ) : (
                        <div className="rounded-md border border-amber-500/20 bg-amber-500/5 p-3 space-y-2">
                          <div className="flex items-start gap-2">
                            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                            <div className="text-sm">
                              <p className="font-medium text-amber-600">
                                {al === undefined ? 'Статус Автозагрузки неизвестен' : 'Автозагрузка не активирована'}
                              </p>
                              <p className="text-xs text-muted-foreground mt-0.5">
                                Для публикации объявлений нужно активировать тариф Avito с Автозагрузкой.
                              </p>
                            </div>
                          </div>
                          <div className="space-y-1.5 sm:pl-6">
                            <p className="text-xs text-muted-foreground">
                              1.{' '}
                              <a
                                href="https://www.avito.ru/autoload/settings"
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-1 text-primary hover:underline"
                              >
                                Откройте настройки Автозагрузки
                                <ExternalLink className="h-3 w-3" />
                              </a>
                            </p>
                            {al?.feed_url && (
                              <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
                                <p className="text-xs text-muted-foreground shrink-0">2. URL фида:</p>
                                <code className="min-w-0 flex-1 break-all rounded bg-muted px-2 py-0.5 text-xs sm:truncate">
                                  {al.feed_url}
                                </code>
                                <button
                                  onClick={() => copyFeedUrl(acc.id, al.feed_url)}
                                  className="shrink-0 text-muted-foreground hover:text-foreground"
                                  title="Скопировать"
                                >
                                  {copiedFeedId === acc.id
                                    ? <Check className="h-3.5 w-3.5 text-green-500" />
                                    : <Copy className="h-3.5 w-3.5" />
                                  }
                                </button>
                              </div>
                            )}
                          </div>
                        </div>
                      )}

                      {health && (
                        <div className="rounded-md border bg-muted/20 p-3 text-sm">
                          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0 flex-1 space-y-1">
                              <p className="font-medium">
                                {health.tariff_status === 'active'
                                  ? health.tariff_name || 'Активный тариф Avito'
                                  : health.tariff_status === 'inactive'
                                    ? 'Тариф Avito неактивен'
                                    : 'Подписка Avito Автозагрузка'}
                              </p>
                              {health.subscription_ends_at ? (
                                <div className="space-y-2 pt-1">
                                  <div className="flex min-w-0 flex-wrap gap-2">
                                    <div className="min-w-[8.5rem] flex-1 rounded-md border bg-background px-3 py-2 sm:flex-none">
                                      <span className="block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                                        Действует до
                                      </span>
                                      <span className="block whitespace-nowrap text-base font-bold tabular-nums tracking-tight sm:text-lg">
                                        {new Date(`${health.subscription_ends_at}T12:00:00`).toLocaleDateString('ru-RU')}
                                      </span>
                                    </div>
                                    {health.days_left !== null && (
                                      <div className={`min-w-[7.5rem] flex-1 rounded-md border px-3 py-2 sm:flex-none ${
                                        health.days_left <= 7
                                          ? 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                                          : 'border-primary/20 bg-primary/5 text-foreground'
                                      }`}
                                      >
                                        <span className="block text-[11px] font-medium uppercase tracking-wide opacity-70">
                                          Осталось
                                        </span>
                                        <span className="block whitespace-nowrap text-base font-bold tabular-nums tracking-tight sm:text-lg">
                                          {health.days_left}{' '}
                                          <span className="text-sm font-semibold">дн.</span>
                                        </span>
                                      </div>
                                    )}
                                  </div>
                                  {health.subscription_source === 'manual' && (
                                    <Badge variant="outline" className="max-w-full whitespace-normal text-[11px] font-normal text-muted-foreground">
                                      Дата указана вручную
                                    </Badge>
                                  )}
                                </div>
                              ) : (
                                <p className="text-xs text-muted-foreground">
                                  Avito API подтверждает статус Автозагрузки, но не передаёт дату её окончания.
                                  Укажите дату продления ниже, чтобы видеть остаток дней.
                                </p>
                              )}
                              {health.placements_remaining !== null && health.placements_total !== null && (
                                <p className="text-xs text-muted-foreground">
                                  Размещений осталось: {health.placements_remaining.toLocaleString('ru-RU')}
                                  {' '}из {health.placements_total.toLocaleString('ru-RU')}
                                </p>
                              )}
                              {health.tariff_price && (
                                <p className="text-xs text-muted-foreground">
                                  Стоимость тарифа: {Number(health.tariff_price).toLocaleString('ru-RU')} ₽
                                </p>
                              )}
                              {health.scheduled_tariff?.name && (
                                <p className="text-xs text-muted-foreground">
                                  Следующий: {health.scheduled_tariff.name}
                                </p>
                              )}
                              {(health.profile_stale
                                || (health.subscription_source === 'avito_tariff' && health.tariff_stale)) && (
                                <p className="text-xs text-amber-600">
                                  Данные устарели — выполните повторную проверку.
                                </p>
                              )}
                              {health.last_error_message && (
                                <p className="text-xs text-amber-600">{health.last_error_message}</p>
                              )}
                              {health.last_attempted_at && (
                                <p className="text-xs text-muted-foreground">
                                  Проверено {new Date(health.last_attempted_at).toLocaleString('ru-RU')}
                                </p>
                              )}
                              {health.subscription_source !== 'avito_tariff' && (
                                <div className="mt-4 flex min-w-0 flex-col gap-2 sm:flex-row sm:items-end">
                                  <div className="min-w-0 flex-1 space-y-1 sm:max-w-56">
                                    <Label htmlFor={`autoload-expiry-${acc.id}`} className="text-xs">
                                      Дата окончания или следующего продления
                                    </Label>
                                    <Input
                                      id={`autoload-expiry-${acc.id}`}
                                      type="date"
                                      value={acc.autoload_subscription_ends_at || ''}
                                      onChange={(event) => updateAccountDraft(acc.id, {
                                        autoload_subscription_ends_at: event.target.value || null,
                                      })}
                                      className="h-9 w-full min-w-0 text-sm font-semibold tabular-nums"
                                    />
                                  </div>
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    className="h-9 w-full sm:w-auto"
                                    onClick={() => saveAutoloadSubscriptionEnd(acc)}
                                    disabled={savingSubscriptionAccountId === acc.id}
                                  >
                                    {savingSubscriptionAccountId === acc.id && (
                                      <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />
                                    )}
                                    Сохранить дату
                                  </Button>
                                </div>
                              )}
                            </div>
                            <a
                              href="https://www.avito.ru/paid-services/listing-fees"
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex shrink-0 items-center gap-1 text-xs text-primary hover:underline"
                            >
                              Управление тарифом
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          </div>
                        </div>
                      )}

                      <div className="flex">
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          onClick={() => checkAutoloadStatus(acc.id)}
                          disabled={checkingAutoloadId === acc.id}
                        >
                          {checkingAutoloadId === acc.id
                            ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />
                            : null}
                          {al === undefined ? 'Проверить Avito' : 'Обновить данные Avito'}
                        </Button>
                      </div>

                      <div className="rounded-md border bg-muted/20 p-3">
                        <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                          <div className="min-w-0">
                            <p className="text-sm font-medium">Размещение по умолчанию</p>
                            <p className="text-xs text-muted-foreground">
                              Эти поля попадут в feed, если у объявления или категории нет своего адреса.
                            </p>
                          </div>
                          <Button
                            size="sm"
                            variant="outline"
                            className="w-full sm:w-auto"
                            onClick={() => saveAccountPlacement(acc)}
                            disabled={savingPlacementAccountId === acc.id}
                          >
                            {savingPlacementAccountId === acc.id && (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            )}
                            Сохранить
                          </Button>
                        </div>
                        <div className="grid gap-3 md:grid-cols-2">
                          <div className="space-y-1">
                            <Label className="text-xs">ID адреса Avito</Label>
                            <Input
                              value={acc.default_seller_address_id || ''}
                              onChange={(e) => updateAccountDraft(acc.id, { default_seller_address_id: e.target.value })}
                              placeholder="ID адреса из Avito Pro"
                              className="h-8 font-mono text-xs"
                            />
                          </div>
                          <div className="space-y-1">
                            <Label className="text-xs">Контактный телефон</Label>
                            <Input
                              value={acc.default_contact_phone || ''}
                              onChange={(e) => updateAccountDraft(acc.id, { default_contact_phone: e.target.value })}
                              placeholder="+7 900 000-00-00"
                              className="h-8 text-xs"
                            />
                          </div>
                          <div className="space-y-1 md:col-span-2">
                            <Label className="text-xs">Адрес</Label>
                            <Input
                              value={acc.default_address || ''}
                              onChange={(e) => updateAccountDraft(acc.id, { default_address: e.target.value })}
                              placeholder="Москва, улица Ленина, 1"
                              className="h-8 text-xs"
                            />
                          </div>
                          <div className="space-y-1 md:col-span-2">
                            <Label className="text-xs">Контактное лицо</Label>
                            <Input
                              value={acc.default_manager_name || ''}
                              onChange={(e) => updateAccountDraft(acc.id, { default_manager_name: e.target.value })}
                              placeholder="Имя менеджера"
                              className="h-8 text-xs"
                            />
                          </div>
                        </div>
                      </div>

                      <div className="rounded-md border bg-muted/20 p-3">
                        <div className="mb-3">
                          <p className="text-sm font-medium">Адреса размещения</p>
                          <p className="text-xs text-muted-foreground">
                            Сохраните адреса из Avito Pro, чтобы выбирать их в объявлениях и массовых действиях.
                          </p>
                        </div>
                        <div className="space-y-2">
                          {accAddresses.length === 0 ? (
                            <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
                              Адресов пока нет. Добавьте хотя бы один адрес для этого аккаунта.
                            </p>
                          ) : accAddresses.map((address) => (
                            <div key={address.id} className="flex flex-wrap items-center gap-2 rounded-md border bg-background px-3 py-2 text-xs">
                              <span className="font-medium">{address.name}</span>
                              {address.is_default && <Badge variant="secondary">По умолчанию</Badge>}
                              {address.seller_address_id && (
                                <code className="rounded bg-muted px-1.5 py-0.5">ID {address.seller_address_id}</code>
                              )}
                              {address.address && <span className="text-muted-foreground">{address.address}</span>}
                              {address.contact_phone && <span className="text-muted-foreground">{address.contact_phone}</span>}
                              <div className="flex w-full items-center gap-1 sm:ml-auto sm:w-auto">
                                {!address.is_default && (
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    className="h-7 px-2 text-xs"
                                    onClick={() => setDefaultPlacementAddress(address)}
                                  >
                                    По умолчанию
                                  </Button>
                                )}
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-7 px-2 text-destructive hover:text-destructive"
                                  onClick={() => deletePlacementAddress(address.id)}
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                            </div>
                          ))}
                        </div>
                        <div className="mt-3 grid gap-2 md:grid-cols-2">
                          <Input
                            value={addressDraft.name}
                            onChange={(e) => updateAddressDraft(acc.id, { name: e.target.value })}
                            placeholder="Название адреса"
                            className="h-8 text-xs"
                          />
                          <Input
                            value={addressDraft.seller_address_id}
                            onChange={(e) => updateAddressDraft(acc.id, { seller_address_id: e.target.value })}
                            placeholder="ID адреса Avito"
                            className="h-8 font-mono text-xs"
                          />
                          <Input
                            value={addressDraft.address}
                            onChange={(e) => updateAddressDraft(acc.id, { address: e.target.value })}
                            placeholder="Регион/адрес размещения"
                            className="h-8 text-xs"
                          />
                          <Input
                            value={addressDraft.contact_phone}
                            onChange={(e) => updateAddressDraft(acc.id, { contact_phone: e.target.value })}
                            placeholder="Контактный телефон"
                            className="h-8 text-xs"
                          />
                          <Input
                            value={addressDraft.manager_name}
                            onChange={(e) => updateAddressDraft(acc.id, { manager_name: e.target.value })}
                            placeholder="Контактное лицо"
                            className="h-8 text-xs"
                          />
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => createPlacementAddress(acc.id)}
                            disabled={savingAddressAccountId === acc.id}
                          >
                            {savingAddressAccountId === acc.id && (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            )}
                            Добавить адрес
                          </Button>
                        </div>
                      </div>
                    </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <CardTitle>Дром</CardTitle>
                  <Badge variant="secondary">Скоро</Badge>
                </div>
                <CardDescription>Заглушка для будущего подключения объявлений на Дром</CardDescription>
              </div>
              <Button size="sm" variant="outline" disabled className="w-full sm:w-auto">
                <Store className="mr-2 h-4 w-4" />
                Недоступно
              </Button>
            </CardHeader>
            <CardContent>
              <div className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">
                Интеграция с Дром пока не подключена. Здесь появятся аккаунты, статусы публикации и настройки синхронизации.
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Источники данных */}
        <TabsContent value="datasources" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Добавить источник данных</CardTitle>
              <CardDescription>
                Подключите несколько складов, баз 1С или файловых выгрузок для одного аккаунта.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleCreateDatasource} className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="datasource-type">Тип источника</Label>
                    <select
                      id="datasource-type"
                      value={newDatasourceType}
                      onChange={(event) => setNewDatasourceType(event.target.value as '1c_http' | '1c_xml')}
                      className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    >
                      <option value="1c_http">1С HTTP-сервис</option>
                      <option value="1c_xml">1С XML выгрузка</option>
                    </select>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="datasource-name">Название</Label>
                    <Input
                      id="datasource-name"
                      placeholder="Основной склад"
                      value={newDatasourceName}
                      onChange={(event) => setNewDatasourceName(event.target.value)}
                      required
                    />
                  </div>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="datasource-url">URL источника</Label>
                  <Input
                    id="datasource-url"
                    placeholder="https://your-1c.server.ru/avito-sync"
                    value={newDatasourceUrl}
                    onChange={(event) => setNewDatasourceUrl(event.target.value)}
                    required
                    className="font-mono text-sm"
                  />
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="datasource-user">Логин</Label>
                    <Input
                      id="datasource-user"
                      value={newDatasourceUser}
                      onChange={(event) => setNewDatasourceUser(event.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="datasource-password">Пароль</Label>
                    <PasswordInput
                      id="datasource-password"
                      value={newDatasourcePassword}
                      onChange={(event) => setNewDatasourcePassword(event.target.value)}
                      required
                    />
                  </div>
                </div>
                <div className="flex justify-end">
                  <Button
                    type="submit"
                    className="w-full sm:w-auto"
                    disabled={
                      creatingDatasource ||
                      !newDatasourceName ||
                      !newDatasourceUrl ||
                      !newDatasourceUser ||
                      !newDatasourcePassword
                    }
                  >
                    {creatingDatasource ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Добавляем...
                      </>
                    ) : (
                      <>
                        <Plus className="mr-2 h-4 w-4" />
                        Добавить источник
                      </>
                    )}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Загрузить прайс-лист (CSV/Excel)</CardTitle>
              <CardDescription>
                Каждый загруженный файл сохраняется как отдельный источник. Обязательные колонки: article, name, price, stock_qty.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleUploadCsv} className="space-y-4">
                <div className="flex items-center justify-center rounded-lg border-2 border-dashed p-8 transition-colors hover:border-primary/50">
                  <label
                    htmlFor="csv-upload"
                    className="flex cursor-pointer flex-col items-center gap-2 text-center"
                  >
                    <Upload className="h-8 w-8 text-muted-foreground" />
                    <span className="text-sm font-medium">
                      {csvFile ? csvFile.name : 'Нажмите для выбора файла'}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      Форматы: .csv, .xls, .xlsx
                    </span>
                    <input
                      id="csv-upload"
                      type="file"
                      accept=".csv,.xlsx,.xls"
                      className="hidden"
                      onChange={(e) => setCsvFile(e.target.files?.[0] || null)}
                    />
                  </label>
                </div>
                <div className="flex justify-end">
                  <Button type="submit" disabled={!csvFile || uploadingCsv} className="w-full sm:w-auto">
                    {uploadingCsv ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Загрузка...
                      </>
                    ) : (
                      'Загрузить файл'
                    )}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Источники данных</CardTitle>
              <CardDescription>Подключённые системы и загруженные файлы</CardDescription>
            </CardHeader>
            <CardContent>
              {loadingDatasources ? (
                <div className="space-y-2">
                  {[1, 2].map((i) => <Skeleton key={i} className="h-16 w-full" />)}
                </div>
              ) : datasources.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  Источников данных нет. Добавьте 1С-источник или загрузите прайс-лист выше.
                </p>
              ) : (
                <div className="space-y-3">
                  {datasources.map((ds) => (
                    <div key={ds.id} className="flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between sm:p-4">
                      <div className="flex min-w-0 items-start gap-3">
                        <div className="mt-1">
                          {ds.type === 'csv' ? (
                            <FileSpreadsheet className="h-5 w-5 text-muted-foreground" />
                          ) : ds.type === '1c_http' ? (
                            <Server className="h-5 w-5 text-muted-foreground" />
                          ) : (
                            <FileCode2 className="h-5 w-5 text-muted-foreground" />
                          )}
                        </div>
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="break-words font-medium">{ds.name}</span>
                            {ds.is_active !== undefined && (
                              <Badge variant={ds.is_active ? 'default' : 'secondary'}>
                                {ds.is_active ? 'Активен' : 'Неактивен'}
                              </Badge>
                            )}
                          </div>
                          <p className="mt-1 break-all text-xs uppercase text-muted-foreground">
                            {ds.type} {ds.url ? `· ${ds.url}` : ''}
                          </p>
                        </div>
                      </div>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-destructive hover:text-destructive"
                        onClick={() => deleteDatasource(ds.id)}
                        disabled={deletingDatasourceId === ds.id}
                      >
                        {deletingDatasourceId === ds.id
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <Trash2 className="h-4 w-4" />
                        }
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Категории каталога */}
        <TabsContent value="catalog-categories" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Категории каталога</CardTitle>
              <CardDescription>
                Вы управляете категориями своего каталога. Тип категории помогает платформе безопасно включать подходящие инструменты обработки.
                Отключите ненужные ветки (например «Для грузовиков и спецтехники» или «Автомобиль на запчасти»), чтобы авто-классификация
                выбирала категории только из вашего ассортимента — отключение действует на всю ветку с подкатегориями.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2 rounded-lg border p-3">
                <p className="text-xs font-medium text-muted-foreground">Направления каталога</p>
                <div className="flex flex-wrap gap-2">
                  {domainOptions.filter((domain) => !['mixed', 'unknown'].includes(domain.slug)).map((domain) => {
                    const isSaving = savingCatalogDomainSlug === domain.slug;
                    return (
                      <Button
                        key={domain.slug}
                        type="button"
                        size="sm"
                        variant={domain.is_enabled_for_tenant ? 'default' : 'outline'}
                        onClick={() => setCatalogDomainEnabled(domain.slug, !domain.is_enabled_for_tenant)}
                        disabled={savingCatalogDomainSlug !== null}
                      >
                        {isSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                        {domainLabel(domain.slug)}
                      </Button>
                    );
                  })}
                </div>
              </div>
              <form className="grid gap-3 md:grid-cols-[1fr_180px_240px_auto]" onSubmit={createCatalogCategory}>
                <Input
                  placeholder="Например: Тормозные колодки"
                  value={newCatalogCategoryName}
                  onChange={(e) => setNewCatalogCategoryName(e.target.value)}
                />
                <select
                  className="rounded-md border bg-background px-3 py-2 text-sm"
                  value={newCatalogCategoryDomain}
                  onChange={(e) => { setNewCatalogCategoryDomain(e.target.value); setNewCatalogCategoryParent(''); }}
                >
                  {enabledDomainOptions.map((domain) => (
                    <option key={domain.slug} value={domain.slug}>{domainLabel(domain.slug)}</option>
                  ))}
                </select>
                <select
                  className="rounded-md border bg-background px-3 py-2 text-sm"
                  value={newCatalogCategoryParent}
                  onChange={(e) => setNewCatalogCategoryParent(e.target.value)}
                  title="Родительская категория (для подкатегории)"
                >
                  <option value="">— как корневую —</option>
                  {buildCategoryTree(
                    catalogCategories.filter((c) => c.root_domain_slug === newCatalogCategoryDomain),
                  ).map(({ category, depth }) => (
                    <option key={category.id} value={category.id}>
                      {'   '.repeat(depth) + category.name}
                    </option>
                  ))}
                </select>
                <Button disabled={creatingCatalogCategory || !newCatalogCategoryName.trim() || enabledDomainOptions.length === 0}>
                  {creatingCatalogCategory
                    ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    : <Plus className="mr-2 h-4 w-4" />
                  }
                  Добавить
                </Button>
              </form>

              {!loadingCatalogCategories && catalogCategories.length > 0 && (
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    className="pl-9"
                    placeholder="Поиск по категориям..."
                    value={categorySearch}
                    onChange={(e) => setCategorySearch(e.target.value)}
                  />
                </div>
              )}

              {loadingCatalogCategories ? (
                <div className="space-y-2">
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                </div>
              ) : catalogCategories.length === 0 ? (
                <div className="rounded-lg border border-dashed p-6 text-center">
                  <p className="text-sm font-medium">Категорий пока нет</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Создайте свои категории и привяжите к ним категории из 1С/CSV.
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {categorySearch && !catalogCategories.some((c) =>
                    c.name.toLowerCase().includes(categorySearch.toLowerCase())
                  ) && (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      Ничего не найдено
                    </p>
                  )}
                  {(categorySearch
                    ? catalogCategories
                        .filter((category) => category.name.toLowerCase().includes(categorySearch.toLowerCase()))
                        .map((category) => ({ category, depth: 0 }))
                    : buildCategoryTree(catalogCategories)
                  ).map(({ category, depth }) => {
                    const isEditing = editingCatalogCategoryId === category.id;
                    const isSaving = savingCatalogCategoryId === category.id;
                    return (
                      <div
                        key={category.id}
                        style={depth ? { marginLeft: depth * 20 } : undefined}
                        className={`flex flex-col gap-3 rounded-lg border p-3 md:flex-row md:items-center md:justify-between${
                          depth ? ' border-l-2 border-l-primary/40 bg-muted/30' : ''
                        }`}
                      >
                        <div className="flex min-w-0 items-center gap-3">
                          <div className="h-14 w-14 shrink-0 overflow-hidden rounded-md border bg-muted">
                            {category.default_image_url ? (
                              // eslint-disable-next-line @next/next/no-img-element
                              <img
                                src={category.default_image_url}
                                alt=""
                                className="h-full w-full object-cover"
                              />
                            ) : (
                              <div className="flex h-full w-full items-center justify-center text-[10px] text-muted-foreground">
                                Нет фото
                              </div>
                            )}
                          </div>
                          {isEditing ? (
                            <div className="grid gap-2 sm:grid-cols-[220px_180px]">
                              <Input
                                value={editCatalogCategoryName}
                                onChange={(e) => setEditCatalogCategoryName(e.target.value)}
                                disabled={isSaving}
                              />
                              <select
                                className="rounded-md border bg-background px-3 py-2 text-sm"
                                value={editCatalogCategoryDomain}
                                onChange={(e) => setEditCatalogCategoryDomain(e.target.value)}
                                disabled={isSaving}
                              >
                                {enabledDomainOptions.map((domain) => (
                                  <option key={domain.slug} value={domain.slug}>
                                    {domainLabel(domain.slug)}
                                  </option>
                                ))}
                              </select>
                            </div>
                          ) : (
                            <div className="min-w-0">
                              <p className="break-words font-medium">{category.name}</p>
                              <p className="text-xs text-muted-foreground">
                                {category.parent
                                  ? `Родитель: ${catalogCategories.find((c) => c.id === category.parent)?.name ?? '—'}`
                                  : `Корень: ${category.root_domain_name || domainLabel(category.domain)}`}
                              </p>
                              {category.default_image_source_name && (
                                <p className="text-xs text-muted-foreground">
                                  Картинка: {category.default_image_source_name}
                                </p>
                              )}
                            </div>
                          )}
                        </div>
                        <div className="flex flex-wrap items-center gap-2">
                          {isEditing ? (
                            <>
                              <Button
                                size="sm"
                                onClick={() => saveCatalogCategoryEdit(category)}
                                disabled={isSaving || !editCatalogCategoryName.trim()}
                              >
                                {isSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                                Сохранить
                              </Button>
                              <Button
                                size="sm"
                                type="button"
                                variant="outline"
                                onClick={() => setEditingCatalogCategoryId(null)}
                                disabled={isSaving}
                              >
                                Отмена
                              </Button>
                            </>
                          ) : (
                            <>
                              <Label className="cursor-pointer rounded-md border px-3 py-2 text-xs">
                                {uploadingCategoryImageId === category.id ? 'Загружаем...' : 'Загрузить картинку'}
                                <input
                                  type="file"
                                  accept="image/*"
                                  className="hidden"
                                  disabled={uploadingCategoryImageId === category.id}
                                  onChange={(e) => uploadCatalogCategoryImage(category.id, e.target.files?.[0] ?? null)}
                                />
                              </Label>
                              {category.default_image_url && (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => deleteCatalogCategoryImage(category.id)}
                                  disabled={uploadingCategoryImageId === category.id}
                                >
                                  Удалить картинку
                                </Button>
                              )}
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => startCatalogCategoryEdit(category)}
                                disabled={isSaving}
                              >
                                Редактировать
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => toggleCatalogCategory(category)}
                                disabled={isSaving}
                                title={category.is_active
                                  ? 'Отключить категорию со всеми подкатегориями — они перестанут участвовать в авто-классификации, товары будут переклассифицированы'
                                  : 'Включить категорию со всеми подкатегориями'}
                              >
                                {isSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                                {category.is_active ? 'Отключить' : 'Включить'}
                              </Button>
                              <Button
                                size="sm"
                                variant="destructive"
                                onClick={() => removeCatalogCategory(category)}
                                disabled={isSaving}
                              >
                                Удалить
                              </Button>
                              <Badge variant={category.is_active ? 'default' : 'secondary'}>
                                {category.is_active ? 'Активна' : 'Отключена'}
                              </Badge>
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Привязка категорий из источника</CardTitle>
              <CardDescription>
                Свяжите категории из 1С/CSV со своими категориями каталога. Сырые данные товара не меняются.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {loadingCatalogCategories ? (
                <Skeleton className="h-24 w-full" />
              ) : sourceCategories.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  В товарах пока нет категорий из источника.
                </p>
              ) : (
                <div className="space-y-2">
                  {sourceCategories.map((source) => {
                    const mapping = catalogMappings.find((item) => item.source_category === source.source_category);
                    const selectedValue = String(mapping?.category ?? source.catalog_category ?? '');
                    const isSaving = savingMappingSource === source.source_category;
                    return (
                      <div
                        key={source.source_category}
                        className="grid gap-2 rounded-lg border p-3 md:grid-cols-[1fr_280px_auto]"
                      >
                        <div>
                          <p className="text-sm font-medium">{source.source_category}</p>
                          <p className="text-xs text-muted-foreground">
                            {mapping ? `Привязана к: ${mapping.category_name}` : 'Категория из 1С/CSV'}
                          </p>
                        </div>
                        <CatalogCategoryPicker
                          categories={catalogCategories}
                          value={selectedValue}
                          disabled={isSaving}
                          onValueChange={(value) => {
                            saveCatalogCategoryMapping(source.source_category, value);
                          }}
                          placeholder="Не привязана"
                        />
                        <Button
                          size="sm"
                          type="button"
                          variant="outline"
                          onClick={() => saveCatalogCategoryMapping(source.source_category, '')}
                          disabled={isSaving || !mapping}
                        >
                          {isSaving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Trash2 className="mr-2 h-4 w-4" />}
                          Убрать
                        </Button>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>

        </TabsContent>

        {/* Наценки */}
        <TabsContent value="pricing" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Наценки по категориям</CardTitle>
              <CardDescription>
                Процент наценки от базовой цены товара. Применяется при создании листинга. Можно скорректировать индивидуально в боковом меню листинга.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <MarginEditor
                categories={catalogCategories.filter((category) => category.is_active)}
                onSaved={loadCatalogCategories}
              />
            </CardContent>
          </Card>
        </TabsContent>

        {/* Уведомления */}
        <TabsContent value="notifications" className="mt-4 space-y-4">
          {/* Telegram */}
          <Card>
            <CardHeader>
              <CardTitle>Telegram-уведомления</CardTitle>
              <CardDescription>
                Получайте алерты об ошибках и важных событиях прямо в Telegram.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {loadingNotif ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-48" />
                </div>
              ) : notifSettings?.telegram_connected ? (
                <div className="space-y-4">
                  <div className="flex flex-col gap-3 rounded-lg border border-green-500/30 bg-green-500/5 p-4 sm:flex-row sm:items-center">
                    <Bell className="h-5 w-5 shrink-0 text-green-600" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-green-700">Telegram подключён</p>
                      {notifSettings.telegram_username && (
                        <p className="text-xs text-muted-foreground">@{notifSettings.telegram_username}</p>
                      )}
                    </div>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={testNotification}
                        disabled={testingNotif}
                      >
                        {testingNotif ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Тест'}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-destructive hover:text-destructive"
                        onClick={disconnectTelegram}
                        disabled={disconnectingTg}
                      >
                        {disconnectingTg
                          ? <Loader2 className="h-4 w-4 animate-spin" />
                          : <BellOff className="h-4 w-4" />
                        }
                      </Button>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="rounded-lg border border-dashed p-6 text-center">
                    <Bell className="mx-auto mb-3 h-8 w-8 text-muted-foreground opacity-50" />
                    <p className="mb-1 text-sm font-medium">Telegram не подключён</p>
                    <p className="mb-4 text-xs text-muted-foreground">
                      Нажмите кнопку, откройте бота и нажмите START — привязка займёт 10 секунд.
                    </p>
                    <Button onClick={connectTelegram} disabled={connectingTg} className="w-full sm:w-auto">
                      {connectingTg
                        ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        : <ExternalLink className="mr-2 h-4 w-4" />
                      }
                      Подключить Telegram
                    </Button>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Email и флаги */}
          <Card>
            <CardHeader>
              <CardTitle>Email и уровни уведомлений</CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              {loadingNotif ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-6 w-64" />
                  <Skeleton className="h-6 w-64" />
                </div>
              ) : (
                <>
                  <div className="space-y-2">
                    <Label>Email для уведомлений</Label>
                    <Input
                      type="email"
                      placeholder={user?.email ?? 'alerts@company.ru'}
                      value={notifEmail}
                      onChange={(e) => setNotifEmail(e.target.value)}
                    />
                    <p className="text-xs text-muted-foreground">
                      Используется для критических событий и уведомлений о биллинге.
                    </p>
                  </div>

                  <Separator />

                  <div className="space-y-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-sm font-medium">Ошибки</p>
                        <p className="text-xs text-muted-foreground">
                          Telegram-уведомления при ошибках публикации
                        </p>
                      </div>
                      <Switch
                        checked={notifOnError}
                        disabled={savingNotif}
                        onCheckedChange={toggleOnError}
                      />
                    </div>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-sm font-medium">Критические события</p>
                        <p className="text-xs text-muted-foreground">
                          Telegram + Email при блокировке аккаунта, исчерпании лимитов
                        </p>
                      </div>
                      <Switch
                        checked={notifOnCritical}
                        disabled={savingNotif}
                        onCheckedChange={toggleOnCritical}
                      />
                    </div>
                  </div>

                  <Button onClick={() => saveNotifSettings()} disabled={savingNotif} className="w-full sm:w-auto">
                    {savingNotif && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    Сохранить email
                  </Button>
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
