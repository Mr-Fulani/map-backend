'use client';

import { useEffect, useState } from 'react';
import { webhookApi } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from 'sonner';
import { Loader2, Plus, Trash2, Zap, Copy, Check, ExternalLink } from 'lucide-react';

interface WebhookEndpoint {
  id: number;
  url: string;
  secret: string;
  events: string[];
  is_active: boolean;
  created_at: string;
}

const EVENT_LABELS: Record<string, string> = {
  'listing.published': 'Листинг опубликован',
  'listing.rejected': 'Листинг отклонён',
  'listing.archived': 'Листинг архивирован',
  'import.completed': 'Импорт завершён',
  'import.failed': 'Ошибка импорта',
  'billing.payment_success': 'Оплата прошла',
  'billing.payment_failed': 'Оплата не прошла',
};

export default function ApiPage() {
  const { tenant } = useAuth();
  const [webhooks, setWebhooks] = useState<WebhookEndpoint[]>([]);
  const [allEvents, setAllEvents] = useState<string[]>([]);
  const [loadingWebhooks, setLoadingWebhooks] = useState(true);
  const [newUrl, setNewUrl] = useState('');
  const [selectedEvents, setSelectedEvents] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [copiedSecret, setCopiedSecret] = useState<number | null>(null);

  useEffect(() => {
    Promise.all([webhookApi.list(), webhookApi.events()])
      .then(([whRes, evRes]) => {
        setWebhooks(whRes.data.data ?? []);
        setAllEvents(evRes.data.data ?? []);
      })
      .catch(() => {})
      .finally(() => setLoadingWebhooks(false));
  }, []);

  function toggleEvent(ev: string) {
    setSelectedEvents((prev) =>
      prev.includes(ev) ? prev.filter((e) => e !== ev) : [...prev, ev]
    );
  }

  async function createWebhook() {
    if (!newUrl.trim() || selectedEvents.length === 0) return;
    setCreating(true);
    try {
      const res = await webhookApi.create({ url: newUrl.trim(), events: selectedEvents });
      setWebhooks((prev) => [res.data.data, ...prev]);
      setNewUrl('');
      setSelectedEvents([]);
      toast.success('Вебхук создан');
    } catch {
      toast.error('Ошибка создания вебхука');
    } finally {
      setCreating(false);
    }
  }

  async function deleteWebhook(id: number) {
    setDeletingId(id);
    try {
      await webhookApi.delete(id);
      setWebhooks((prev) => prev.filter((w) => w.id !== id));
      toast.success('Вебхук удалён');
    } catch {
      toast.error('Ошибка удаления');
    } finally {
      setDeletingId(null);
    }
  }

  async function testWebhook(id: number) {
    setTestingId(id);
    try {
      const res = await webhookApi.test(id);
      const ok = res.data.data?.ok;
      if (ok) {
        toast.success(`Тест успешен (HTTP ${res.data.data.http_status})`);
      } else {
        toast.error(`Сервер вернул HTTP ${res.data.data?.http_status}`);
      }
    } catch {
      toast.error('Сервер недоступен или вернул ошибку');
    } finally {
      setTestingId(null);
    }
  }

  function copySecret(id: number, secret: string) {
    navigator.clipboard.writeText(secret);
    setCopiedSecret(id);
    setTimeout(() => setCopiedSecret(null), 2000);
  }

  const apiBaseUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">API & Webhooks</h1>
        <p className="text-muted-foreground">Интеграция с MAP API и настройка вебхуков</p>
      </div>

      <Tabs defaultValue="webhooks">
        <div className="overflow-x-auto">
          <TabsList className="w-full sm:w-auto">
            <TabsTrigger value="webhooks">Вебхуки</TabsTrigger>
            <TabsTrigger value="docs">Документация API</TabsTrigger>
            <TabsTrigger value="examples">Примеры кода</TabsTrigger>
          </TabsList>
        </div>

        {/* Вебхуки */}
        <TabsContent value="webhooks" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Вебхук-эндпоинты</CardTitle>
              <CardDescription>
                MAP отправит POST-запрос на ваш URL при наступлении событий.
                Запросы подписаны HMAC-SHA256 через заголовок <code className="text-xs">X-MAP-Signature</code>.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              {/* Форма создания */}
              <div className="space-y-3">
                <Input
                  placeholder="https://your-app.com/webhooks/map"
                  value={newUrl}
                  onChange={(e) => setNewUrl(e.target.value)}
                />
                <div>
                  <p className="mb-2 text-xs text-muted-foreground">Выберите события:</p>
                  <div className="flex flex-wrap gap-2">
                    {allEvents.map((ev) => (
                      <Badge
                        key={ev}
                        variant={selectedEvents.includes(ev) ? 'default' : 'outline'}
                        className="cursor-pointer select-none"
                        onClick={() => toggleEvent(ev)}
                      >
                        {EVENT_LABELS[ev] ?? ev}
                      </Badge>
                    ))}
                  </div>
                </div>
                <Button
                  onClick={createWebhook}
                  disabled={creating || !newUrl.trim() || selectedEvents.length === 0}
                >
                  {creating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
                  Добавить вебхук
                </Button>
              </div>

              <Separator />

              {/* Список */}
              {loadingWebhooks ? (
                <div className="space-y-3">
                  {[1, 2].map((i) => <Skeleton key={i} className="h-20 w-full" />)}
                </div>
              ) : webhooks.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  Вебхуков нет. Добавьте первый выше.
                </p>
              ) : (
                <div className="space-y-3">
                  {webhooks.map((wh) => (
                    <div key={wh.id} className="rounded-lg border p-4">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0 flex-1">
                          <p className="truncate font-mono text-sm font-medium">{wh.url}</p>
                          <div className="mt-1 flex flex-wrap gap-1">
                            {wh.events.map((ev) => (
                              <Badge key={ev} variant="secondary" className="text-xs">
                                {EVENT_LABELS[ev] ?? ev}
                              </Badge>
                            ))}
                          </div>
                          <div className="mt-2 flex items-center gap-2">
                            <code className="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">
                              {wh.secret.slice(0, 8)}...{wh.secret.slice(-4)}
                            </code>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-6 px-1"
                              onClick={() => copySecret(wh.id, wh.secret)}
                            >
                              {copiedSecret === wh.id
                                ? <Check className="h-3 w-3 text-green-500" />
                                : <Copy className="h-3 w-3" />
                              }
                            </Button>
                          </div>
                        </div>
                        <div className="flex shrink-0 gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => testWebhook(wh.id)}
                            disabled={testingId === wh.id}
                          >
                            {testingId === wh.id
                              ? <Loader2 className="h-3 w-3 animate-spin" />
                              : <Zap className="h-3 w-3" />
                            }
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-destructive hover:text-destructive"
                            onClick={() => deleteWebhook(wh.id)}
                            disabled={deletingId === wh.id}
                          >
                            {deletingId === wh.id
                              ? <Loader2 className="h-3 w-3 animate-spin" />
                              : <Trash2 className="h-3 w-3" />
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

          {/* Формат payload */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Формат payload</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto rounded bg-muted p-4 font-mono text-xs">
{`POST https://your-app.com/webhooks/map
Content-Type: application/json
X-MAP-Signature: sha256=<hmac_sha256_hex>
X-MAP-Event: listing.published

{
  "event": "listing.published",
  "tenant": "${tenant?.slug ?? 'your-slug'}",
  "data": {
    "listing_id": 42,
    "product_article": "SKU-001",
    "external_url": "https://avito.ru/..."
  }
}`}
              </pre>
              <p className="mt-3 text-xs text-muted-foreground">
                Верификация подписи: <code>HMAC-SHA256(secret, raw_body)</code>.
                Убедитесь, что читаете raw body до парсинга JSON.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Документация */}
        <TabsContent value="docs" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Swagger / OpenAPI</CardTitle>
              <CardDescription>Интерактивная документация со всеми эндпоинтами и схемами</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <a
                href={`${apiBaseUrl}/api/docs/`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 text-sm text-primary hover:underline"
              >
                Открыть Swagger UI
                <ExternalLink className="h-3 w-3" />
              </a>
              <Separator />
              <div className="space-y-2">
                <p className="text-sm font-medium">Базовый URL</p>
                <code className="block rounded bg-muted px-3 py-2 font-mono text-xs">{apiBaseUrl}/api/v1/</code>
              </div>
              <div className="space-y-2">
                <p className="text-sm font-medium">Аутентификация</p>
                <pre className="rounded bg-muted p-3 font-mono text-xs">
{`Authorization: Bearer map_sk_<ваш_ключ>
# или JWT:
Authorization: Bearer <access_token>`}
                </pre>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Примеры кода */}
        <TabsContent value="examples" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>cURL</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto rounded bg-muted p-4 font-mono text-xs">
{`# Получить список товаров
curl -X GET "${apiBaseUrl}/api/v1/products/" \\
  -H "Authorization: Bearer map_sk_YOUR_KEY"

# Получить аналитику за 30 дней
curl -X GET "${apiBaseUrl}/api/v1/analytics/?date_from=2024-01-01&date_to=2024-01-31" \\
  -H "Authorization: Bearer map_sk_YOUR_KEY"

# Получить листинги
curl -X GET "${apiBaseUrl}/api/v1/listings/?status=active" \\
  -H "Authorization: Bearer map_sk_YOUR_KEY"`}
              </pre>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Python</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto rounded bg-muted p-4 font-mono text-xs">
{`import requests

API_KEY = "map_sk_YOUR_KEY"
BASE_URL = "${apiBaseUrl}/api/v1"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# Список товаров
resp = requests.get(f"{BASE_URL}/products/", headers=HEADERS)
products = resp.json()["data"]

# Верификация вебхука
import hmac, hashlib

def verify_webhook(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)`}
              </pre>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>JavaScript / TypeScript</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto rounded bg-muted p-4 font-mono text-xs">
{`const API_KEY = "map_sk_YOUR_KEY";
const BASE_URL = "${apiBaseUrl}/api/v1";

// Получить список листингов
const resp = await fetch(\`\${BASE_URL}/listings/?status=active\`, {
  headers: { Authorization: \`Bearer \${API_KEY}\` },
});
const { data } = await resp.json();

// Верификация вебхука (Node.js)
import { createHmac } from "crypto";

function verifyWebhook(rawBody: Buffer, signature: string, secret: string): boolean {
  const expected = createHmac("sha256", secret).update(rawBody).digest("hex");
  return signature === \`sha256=\${expected}\`;
}`}
              </pre>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
