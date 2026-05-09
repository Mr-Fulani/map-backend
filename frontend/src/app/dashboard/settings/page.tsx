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
import { Loader2, Plus, Trash2, Copy, Check, ExternalLink, Bell, BellOff, KeyRound } from 'lucide-react';

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

interface NotificationSettings {
  telegram_connected: boolean;
  telegram_username: string;
  notify_email: string;
  notify_on_error: boolean;
  notify_on_critical: boolean;
}

const SETTINGS_TABS = ['organization', 'api-keys', 'accounts', 'notifications'] as const;
type SettingsTab = typeof SETTINGS_TABS[number];

export default function SettingsPage() {
  const { user, tenant } = useAuth();
  const [activeTab, setActiveTab] = useState<SettingsTab>('organization');
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loadingKeys, setLoadingKeys] = useState(true);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [newKeyName, setNewKeyName] = useState('');
  const [creatingKey, setCreatingKey] = useState(false);
  const [newKeyValue, setNewKeyValue] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokingId, setRevokingId] = useState<number | null>(null);
  const [deletingAccountId, setDeletingAccountId] = useState<number | null>(null);

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

  useEffect(() => {
    tenantApi.getApiKeys()
      .then((r) => setApiKeys(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingKeys(false));

    accountApi.list()
      .then((r) => setAccounts(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingAccounts(false));

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
            <TabsTrigger value="organization">Организация</TabsTrigger>
            <TabsTrigger value="api-keys">API-ключи</TabsTrigger>
            <TabsTrigger value="accounts">Avito-аккаунты</TabsTrigger>
            <TabsTrigger value="notifications">Уведомления</TabsTrigger>
          </TabsList>
        </div>

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
        <TabsContent value="accounts" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Avito-аккаунты</CardTitle>
              <CardDescription>Подключённые аккаунты маркетплейсов</CardDescription>
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
                    <div key={acc.id} className="flex items-center justify-between rounded-lg border p-4">
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{acc.name}</span>
                          <Badge variant={acc.is_active ? 'default' : 'secondary'}>
                            {acc.is_active ? 'Активен' : 'Неактивен'}
                          </Badge>
                        </div>
                        <p className="mt-1 font-mono text-xs text-muted-foreground">
                          {acc.marketplace} · ID: {acc.external_id}
                        </p>
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
