'use client';

import { useState } from 'react';
import { AlertCircle, CheckCircle2, ExternalLink, KeyRound, Loader2, Plus, Store } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { PasswordInput } from '@/components/ui/password-input';
import { OzonCatalogStatus } from '@/components/marketplaces/OzonCatalogStatus';
import { accountApi } from '@/lib/api';
import type { MarketplaceAccount } from '@/lib/marketplace-account-types';
import {
  ozonConnectionPresentation,
  ozonKeyExpiryPresentation,
  ozonOnboardingErrorMessage,
  type OzonStatusTone,
} from '@/lib/ozon-account-presentation';

interface OzonAccountSettingsProps {
  accounts: MarketplaceAccount[];
  loading: boolean;
  canManage: boolean;
  connectionEnabled: boolean;
  credentialUpdateEnabled: boolean;
  onAccountUpsert: (account: MarketplaceAccount) => void;
}

const TONE_CLASSES: Record<OzonStatusTone, string> = {
  success: 'border-green-500/20 bg-green-500/5 text-green-700 dark:text-green-400',
  warning: 'border-amber-500/20 bg-amber-500/5 text-amber-700 dark:text-amber-400',
  danger: 'border-red-500/20 bg-red-500/5 text-red-700 dark:text-red-400',
  neutral: 'border-border bg-muted/30 text-foreground',
};

function accountResponse(body: unknown): MarketplaceAccount {
  if (body && typeof body === 'object' && 'data' in body) {
    return (body as { data: MarketplaceAccount }).data;
  }
  return body as MarketplaceAccount;
}

function safeDate(value: string): string {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString('ru-RU')
    : 'неизвестно';
}

export function OzonAccountSettings({
  accounts,
  loading,
  canManage,
  connectionEnabled,
  credentialUpdateEnabled,
  onAccountUpsert,
}: OzonAccountSettingsProps) {
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [creating, setCreating] = useState(false);
  const [rotatingAccountId, setRotatingAccountId] = useState<number | null>(null);
  const [showRotationFor, setShowRotationFor] = useState<number | null>(null);

  async function createAccount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connectionEnabled || !canManage) return;

    const form = event.currentTarget;
    const data = new FormData(form);
    const name = String(data.get('name') || '').trim();
    const clientId = String(data.get('client_id') || '').trim();
    const apiKey = String(data.get('api_key') || '').trim();
    const confirmed = data.get('confirm_ozon_read_only_access') === 'on';
    form.reset();

    if (!name || !clientId || !apiKey) {
      toast.error('Укажите название, Client ID и API-ключ Ozon.');
      return;
    }
    if (!confirmed) {
      toast.error('Подтвердите read-only проверку кабинета Ozon.');
      return;
    }

    // Unmount PasswordInput before the network wait so its internal display
    // state cannot retain the submitted secret after this event turn.
    setShowCreateForm(false);
    setCreating(true);
    try {
      const response = await accountApi.create({
        marketplace: 'ozon',
        name,
        client_id: clientId,
        api_key: apiKey,
        confirm_ozon_read_only_access: true,
      });
      onAccountUpsert(accountResponse(response.data));
      toast.success('Аккаунт Ozon проверен и подключён.');
    } catch (error: unknown) {
      toast.error(ozonOnboardingErrorMessage(error));
    } finally {
      setCreating(false);
    }
  }

  async function rotateCredentials(
    event: React.FormEvent<HTMLFormElement>,
    account: MarketplaceAccount,
  ) {
    event.preventDefault();
    if (!credentialUpdateEnabled || !canManage) return;

    const form = event.currentTarget;
    const data = new FormData(form);
    const apiKey = String(data.get('api_key') || '').trim();
    const confirmed = data.get('confirm_ozon_read_only_access') === 'on';
    form.reset();
    if (!apiKey) {
      toast.error('Укажите новый API-ключ Ozon.');
      return;
    }
    if (!confirmed) {
      toast.error('Подтвердите read-only проверку нового ключа Ozon.');
      return;
    }

    setShowRotationFor(null);
    setRotatingAccountId(account.id);
    try {
      const response = await accountApi.replaceCredentials(account.id, {
        marketplace: 'ozon',
        name: account.name,
        client_id: account.external_id,
        api_key: apiKey,
        confirm_ozon_read_only_access: true,
      });
      onAccountUpsert(accountResponse(response.data));
      toast.success('Новый API-ключ проверен и сохранён.');
    } catch (error: unknown) {
      toast.error(ozonOnboardingErrorMessage(error));
    } finally {
      setRotatingAccountId(null);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle>Ozon</CardTitle>
            <Badge variant={connectionEnabled ? 'default' : 'secondary'}>
              {connectionEnabled ? 'Подключение разрешено' : 'Интерфейс готов · rollout закрыт'}
            </Badge>
          </div>
          <CardDescription>
            Отдельные кабинеты, API-права, срок ключа и один FBS-склад
          </CardDescription>
        </div>
        <Button
          size="sm"
          className="w-full sm:w-auto"
          disabled={!connectionEnabled || !canManage || showCreateForm || creating}
          onClick={() => setShowCreateForm(true)}
          title={!connectionEnabled ? 'Подключение откроется отдельным canary-этапом' : undefined}
        >
          <Plus className="mr-2 h-4 w-4" />
          {creating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {creating ? 'Проверяем…' : 'Добавить Ozon'}
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {!connectionEnabled && (
          <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4 text-sm">
            <div className="flex items-start gap-2">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div className="space-y-1">
                <p className="font-medium text-amber-700 dark:text-amber-400">
                  Подключение кабинета пока выключено
                </p>
                <p className="text-xs text-muted-foreground">
                  UI и backend-контракт выложены, но MAP не принимает API-ключ и не делает запросов
                  в Ozon до отдельного разрешения read-only canary.
                </p>
              </div>
            </div>
          </div>
        )}

        {showCreateForm && connectionEnabled && (
          <form onSubmit={createAccount} className="space-y-4 rounded-lg border p-4" autoComplete="off">
            <div className="flex items-start gap-2 rounded-md border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
              <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
              <p>
                API-ключ отправляется один раз для server-to-server проверки, не сохраняется в
                состоянии страницы и никогда не возвращается из API MAP.
              </p>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2 md:col-span-2">
                <Label htmlFor="ozon-account-name">Название аккаунта</Label>
                <Input id="ozon-account-name" name="name" maxLength={200} required placeholder="Например: AlfaPro Ozon" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="ozon-client-id">Client ID</Label>
                <Input id="ozon-client-id" name="client_id" maxLength={100} required className="font-mono" autoComplete="off" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="ozon-api-key">API-ключ</Label>
                <PasswordInput id="ozon-api-key" name="api_key" maxLength={1000} required className="font-mono" autoComplete="new-password" />
              </div>
            </div>
            <label className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-muted-foreground">
              <input
                type="checkbox"
                name="confirm_ozon_read_only_access"
                required
                className="mt-0.5 h-4 w-4 shrink-0"
              />
              <span>
                Подтверждаю однократную read-only проверку ролей API, данных продавца и списка
                складов. MAP не будет создавать или изменять товары, цены и остатки.
              </span>
            </label>
            <div className="flex flex-col gap-2 sm:flex-row sm:justify-between">
              <a
                href="https://seller.ozon.ru/app/settings/api-keys"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
              >
                API-ключи в Ozon Seller
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
              <div className="flex gap-2">
                <Button type="button" variant="outline" onClick={() => setShowCreateForm(false)} disabled={creating}>
                  Отмена
                </Button>
                <Button type="submit" disabled={creating}>
                  {creating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Проверить и подключить
                </Button>
              </div>
            </div>
          </form>
        )}

        {loading ? (
          <div className="space-y-2">
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
          </div>
        ) : accounts.length === 0 ? (
          <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            <Store className="mx-auto mb-2 h-5 w-5" />
            Ozon-аккаунты ещё не подключены. Один tenant сможет хранить несколько кабинетов.
          </div>
        ) : (
          <div className="space-y-3">
            {accounts.map((account) => {
              const profile = account.ozon_profile;
              const connection = ozonConnectionPresentation(profile);
              const expiry = ozonKeyExpiryPresentation(profile?.api_key_expires_at ?? null);
              return (
                <div key={account.id} className="space-y-4 rounded-lg border p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="break-words font-medium">{account.name}</p>
                        <Badge variant={account.is_active ? 'default' : 'secondary'}>
                          {account.is_active ? 'Активен' : 'Неактивен'}
                        </Badge>
                      </div>
                      <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
                        Client ID: {account.external_id}
                      </p>
                      {(profile?.company_name || profile?.seller_name) && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {[profile.company_name, profile.seller_name].filter(Boolean).join(' · ')}
                        </p>
                      )}
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={
                        !credentialUpdateEnabled
                        || !canManage
                        || showRotationFor === account.id
                        || rotatingAccountId === account.id
                      }
                      onClick={() => setShowRotationFor(account.id)}
                    >
                      {rotatingAccountId === account.id
                        ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                        : <KeyRound className="mr-2 h-3.5 w-3.5" />}
                      {rotatingAccountId === account.id ? 'Проверяем…' : 'Обновить ключ'}
                    </Button>
                  </div>

                  <div className="grid gap-3 lg:grid-cols-2">
                    {[connection, expiry].map((item) => (
                      <div key={item.label} className={`rounded-md border p-3 text-sm ${TONE_CLASSES[item.tone]}`}>
                        <div className="flex items-start gap-2">
                          {item.tone === 'success'
                            ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                            : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />}
                          <div>
                            <p className="font-medium">{item.label}</p>
                            <p className="mt-0.5 text-xs opacity-80">{item.description}</p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>

                  {profile && (
                    <div className="grid gap-3 text-sm md:grid-cols-2">
                      <div className="rounded-md border bg-muted/20 p-3">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">FBS-склад</p>
                        <p className="mt-1 font-medium">
                          {profile.selected_warehouse_name || 'Не выбран'}
                        </p>
                        {profile.selected_warehouse_id && (
                          <p className="mt-0.5 break-all font-mono text-xs text-muted-foreground">
                            ID {profile.selected_warehouse_id}
                          </p>
                        )}
                      </div>
                      <div className="rounded-md border bg-muted/20 p-3">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">API-права</p>
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {profile.roles.length > 0
                            ? profile.roles.map((role) => <Badge key={role} variant="outline">{role}</Badge>)
                            : <span className="text-xs text-muted-foreground">Роли не переданы</span>}
                        </div>
                        <p className="mt-2 text-xs text-muted-foreground">
                          Разрешённых методов: {profile.api_methods.length}
                        </p>
                      </div>
                      <p className="text-xs text-muted-foreground md:col-span-2">
                        Проверено: {safeDate(profile.last_checked_at)}
                      </p>
                    </div>
                  )}

                  <OzonCatalogStatus
                    accountId={account.id}
                    accountName={account.name}
                    accountActive={account.is_active}
                    canManage={canManage}
                    connectionEnabled={connectionEnabled}
                  />

                  {showRotationFor === account.id && credentialUpdateEnabled && (
                    <form onSubmit={(event) => rotateCredentials(event, account)} className="space-y-3 rounded-md border p-3" autoComplete="off">
                      <div className="space-y-2">
                        <Label htmlFor={`ozon-api-key-${account.id}`}>Новый API-ключ</Label>
                        <PasswordInput
                          id={`ozon-api-key-${account.id}`}
                          name="api_key"
                          maxLength={1000}
                          required
                          className="font-mono"
                          autoComplete="new-password"
                        />
                      </div>
                      <label className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-muted-foreground">
                        <input
                          type="checkbox"
                          name="confirm_ozon_read_only_access"
                          required
                          className="mt-0.5 h-4 w-4 shrink-0"
                        />
                        <span>
                          Подтверждаю read-only проверку нового ключа без изменений товаров,
                          цен и остатков в Ozon.
                        </span>
                      </label>
                      <div className="flex justify-end gap-2">
                        <Button type="button" variant="outline" onClick={() => setShowRotationFor(null)} disabled={rotatingAccountId === account.id}>
                          Отмена
                        </Button>
                        <Button type="submit" disabled={rotatingAccountId === account.id}>
                          {rotatingAccountId === account.id && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                          Проверить и заменить
                        </Button>
                      </div>
                    </form>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
