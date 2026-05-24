'use client';

import { useEffect, useRef, useState } from 'react';
import { tenantApi, accountApi, notificationApi } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Switch } from '@/components/ui/switch';
import { toast } from 'sonner';
import { Loader2, Plus, Trash2, Copy, Check, ExternalLink, Bell, BellOff, KeyRound, Eye, EyeOff, Upload, FileSpreadsheet, Server, FileCode2 } from 'lucide-react';
import { profileApi, datasourceApi } from '@/lib/api';

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
  created_at: string;
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

const SETTINGS_TABS = ['profile', 'organization', 'api-keys', 'accounts', 'datasources', 'notifications'] as const;
type SettingsTab = typeof SETTINGS_TABS[number];

export default function SettingsPage() {
  const { user, tenant } = useAuth();
  const [activeTab, setActiveTab] = useState<SettingsTab>('profile');
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [loadingKeys, setLoadingKeys] = useState(true);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [loadingDatasources, setLoadingDatasources] = useState(true);
  const [newKeyName, setNewKeyName] = useState('');
  const [creatingKey, setCreatingKey] = useState(false);
  const [newKeyValue, setNewKeyValue] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokingId, setRevokingId] = useState<number | null>(null);
  const [deletingAccountId, setDeletingAccountId] = useState<number | null>(null);
  const [togglingAccountId, setTogglingAccountId] = useState<number | null>(null);
  const [editingAccountId, setEditingAccountId] = useState<number | null>(null);
  const [editAccountName, setEditAccountName] = useState('');
  const [deletingDatasourceId, setDeletingDatasourceId] = useState<number | null>(null);
  
  // File upload state
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [uploadingCsv, setUploadingCsv] = useState(false);
  
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
      const hash = window.location.hash.slice(1) as SettingsTab;
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
  const [showPasswords, setShowPasswords] = useState(false);
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

  // Подставляем телефон из данных пользователя при загрузке
  useEffect(() => {
    if (user && 'phone' in user) {
      setPhone((user as { phone?: string }).phone ?? '');
    }
  }, [user]);

  useEffect(() => {
    tenantApi.getApiKeys()
      .then((r) => setApiKeys(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingKeys(false));

    accountApi.list()
      .then((r) => setAccounts(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingAccounts(false));

    datasourceApi.list()
      .then((r) => setDatasources(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingDatasources(false));

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
  }, []);

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
        toast.error('Аккаунт с таким Client ID уже существует');
        return;
      }
      toast.error('Ошибка при добавлении аккаунта');
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

  async function handleUploadCsv(e: React.FormEvent) {
    e.preventDefault();
    if (!csvFile) return;
    setUploadingCsv(true);
    try {
      const { data: uploadResult } = await datasourceApi.uploadCsv(csvFile);
      toast.success(`Файл загружен: ${uploadResult.data?.rows_count || uploadResult.items?.length || 0} строк`);
      setCsvFile(null);
      
      const r = await datasourceApi.list();
      setDatasources(r.data.data ?? r.data);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string; message?: string } } };
      const message = axiosErr?.response?.data?.detail || axiosErr?.response?.data?.message || 'Ошибка загрузки файла';
      toast.error(message);
    } finally {
      setUploadingCsv(false);
    }
  }

  async function connectTelegram() {
    setConnectingTg(true);
    try {
      const res = await notificationApi.telegramConnect();
      const botUrl = res.data.data?.bot_url as string;
      window.open(botUrl, '_blank');
      toast.info('Откройте бота в Telegram и нажмите START. После привязки обновите страницу.');
    } catch {
      toast.error('Не удалось создать ссылку. Проверьте настройки бота на сервере.');
    } finally {
      setConnectingTg(false);
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
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Настройки</h1>
        <p className="text-muted-foreground">Управление организацией и интеграциями</p>
      </div>

      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as SettingsTab)}>
        <div className="overflow-x-auto">
          <TabsList className="w-full sm:w-auto">
            <TabsTrigger value="profile">Профиль</TabsTrigger>
            <TabsTrigger value="organization">Организация</TabsTrigger>
            <TabsTrigger value="api-keys">API-ключи</TabsTrigger>
            <TabsTrigger value="accounts">Avito-аккаунты</TabsTrigger>
            <TabsTrigger value="datasources">Источники данных</TabsTrigger>
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
                  <div className="flex gap-2">
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
                <div className="flex gap-2">
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
                  <div className="relative">
                    <Input
                      type={showPasswords ? 'text' : 'password'}
                      value={currentPassword}
                      onChange={(e) => setCurrentPassword(e.target.value)}
                      className="pr-10"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPasswords((v) => !v)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                    >
                      {showPasswords ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>Новый пароль</Label>
                  <Input
                    type={showPasswords ? 'text' : 'password'}
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="Минимум 8 символов"
                  />
                </div>
                <div className="space-y-2">
                  <Label>Подтвердите новый пароль</Label>
                  <Input
                    type={showPasswords ? 'text' : 'password'}
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
                  <div className="flex items-center gap-2">
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
              <div className="flex gap-2">
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
                    <div key={key.id} className="flex items-center justify-between rounded-lg border p-3 gap-3">
                      <div className="flex items-start gap-3 min-w-0">
                        <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        <div className="min-w-0">
                          <p className="text-sm font-medium">{key.name}</p>
                          <code className="text-xs font-mono text-muted-foreground bg-muted rounded px-1.5 py-0.5 mt-0.5 inline-block">
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

        {/* Avito аккаунты */}
        <TabsContent value="accounts" className="mt-4 space-y-4">
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
                    <Input 
                      type="password"
                      placeholder="Client Secret" 
                      value={newClientSecret}
                      onChange={(e) => setNewClientSecret(e.target.value)}
                      required
                      className="font-mono"
                    />
                  </div>
                  <div className="flex justify-end gap-2">
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
            <CardHeader className="flex flex-row items-center justify-between">
              <div>
                <CardTitle>Avito-аккаунты</CardTitle>
                <CardDescription>Подключённые аккаунты маркетплейсов</CardDescription>
              </div>
              {!showAddAccount && (
                <Button onClick={() => setShowAddAccount(true)} size="sm">
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
                  Аккаунтов нет. Добавьте через онбординг.
                </p>
              ) : (
                <div className="space-y-3">
                  {accounts.map((acc) => (
                    <div key={acc.id} className="rounded-lg border p-4 space-y-3">
                      <div className="flex items-center justify-between gap-3">
                        <div className="flex-1 min-w-0">
                          {editingAccountId === acc.id ? (
                            <div className="flex items-center gap-2">
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
                              className="font-medium text-left hover:underline"
                              onClick={() => { setEditingAccountId(acc.id); setEditAccountName(acc.name); }}
                            >
                              {acc.name}
                            </button>
                          )}
                          <p className="mt-1 font-mono text-xs text-muted-foreground">
                            {acc.marketplace} · ID: {acc.external_id}
                          </p>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
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
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Источники данных */}
        <TabsContent value="datasources" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Загрузить прайс-лист (CSV/Excel)</CardTitle>
              <CardDescription>
                Ручная загрузка файла с товарами. Обязательные колонки: article, name, price, stock_qty.
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
                  <Button type="submit" disabled={!csvFile || uploadingCsv}>
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
                  Источников данных нет.
                </p>
              ) : (
                <div className="space-y-3">
                  {datasources.map((ds) => (
                    <div key={ds.id} className="flex items-center justify-between rounded-lg border p-4">
                      <div className="flex items-start gap-3">
                        <div className="mt-1">
                          {ds.type === 'csv' ? (
                            <FileSpreadsheet className="h-5 w-5 text-muted-foreground" />
                          ) : ds.type === '1c_http' ? (
                            <Server className="h-5 w-5 text-muted-foreground" />
                          ) : (
                            <FileCode2 className="h-5 w-5 text-muted-foreground" />
                          )}
                        </div>
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{ds.name}</span>
                            {ds.is_active !== undefined && (
                              <Badge variant={ds.is_active ? 'default' : 'secondary'}>
                                {ds.is_active ? 'Активен' : 'Неактивен'}
                              </Badge>
                            )}
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground uppercase">
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
                  <div className="flex items-center gap-3 rounded-lg border border-green-500/30 bg-green-500/5 p-4">
                    <Bell className="h-5 w-5 shrink-0 text-green-600" />
                    <div className="flex-1">
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
                    <Button onClick={connectTelegram} disabled={connectingTg}>
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
                    <div className="flex items-center justify-between">
                      <div>
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
                    <div className="flex items-center justify-between">
                      <div>
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

                  <Button onClick={() => saveNotifSettings()} disabled={savingNotif}>
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
