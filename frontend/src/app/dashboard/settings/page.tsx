'use client';

import { useEffect, useState } from 'react';
import { tenantApi, accountApi } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from 'sonner';
import { Loader2, Plus, Trash2, Copy, Check } from 'lucide-react';

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

export default function SettingsPage() {
  const { user, tenant } = useAuth();
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

  useEffect(() => {
    tenantApi.getApiKeys()
      .then((r) => setApiKeys(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingKeys(false));

    accountApi.list()
      .then((r) => setAccounts(r.data.data ?? r.data))
      .catch(() => {})
      .finally(() => setLoadingAccounts(false));
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

      <Tabs defaultValue="organization">
        <TabsList>
          <TabsTrigger value="organization">Организация</TabsTrigger>
          <TabsTrigger value="api-keys">API-ключи</TabsTrigger>
          <TabsTrigger value="accounts">Avito-аккаунты</TabsTrigger>
        </TabsList>

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
              <CardDescription>
                Используются для прямого доступа к API без JWT. Показываются только при создании.
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
                    <div key={key.id} className="flex items-center justify-between rounded-lg border p-3">
                      <div>
                        <p className="text-sm font-medium">{key.name}</p>
                        <p className="font-mono text-xs text-muted-foreground">
                          {key.prefix}...
                          {key.last_used_at && (
                            <span className="ml-2">
                              использован {new Date(key.last_used_at).toLocaleDateString('ru-RU')}
                            </span>
                          )}
                        </p>
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
      </Tabs>
    </div>
  );
}
