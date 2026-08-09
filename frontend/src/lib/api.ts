/**
 * API-клиент для взаимодействия с Django backend.
 * Access/CSRF живут только в памяти, refresh — только в HttpOnly cookie.
 */

import axios, { type AxiosRequestConfig } from 'axios';
import {
  advanceBrowserSessionVersion,
  readBrowserSessionVersion,
  requireBrowserSessionStorage,
  requireBrowserSessionVersion,
  subscribeToBrowserSessionVersion,
  type BrowserSessionLockGuard,
  type BrowserSessionVersion,
  withBrowserSessionLock,
} from './browser-session-lock';

// Пустое значение означает same-origin: в production Nginx проксирует /api/ в Django.
export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_URL || '').replace(/\/$/, '');
const BILLING_READ_TIMEOUT_MS = 10_000;
const BILLING_MUTATION_TIMEOUT_MS = 30_000;

const api = axios.create({
  baseURL: `${API_BASE_URL}/api/v1`,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
});

// Auth endpoints use an isolated transport without the application interceptors.
// This prevents a failed refresh/login request from recursively refreshing itself.
const authTransport = axios.create({
  baseURL: `${API_BASE_URL}/api/v1`,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
  timeout: 10_000,
});

// === In-memory browser session ===

let accessToken: string | null = null;
let csrfToken: string | null = null;
let csrfPromise: Promise<string> | null = null;
let refreshPromise: Promise<string> | null = null;
let authChannel: BroadcastChannel | null = null;
let authChannelUnavailable = false;
let storageSessionSignalsReady = false;
let sessionVersion = readBrowserSessionVersion();

const AUTH_EXPIRED_EVENT = 'map:auth-expired';
const AUTH_REPLACED_EVENT = 'map:auth-replaced';
const AUTH_CHANNEL_NAME = 'map:browser-session';

type AuthChannelMessage =
  | {
      type: 'active';
      access: string;
      revision: number;
      sequence: number;
      sessionId: string;
    }
  | {
      type: 'cleared';
      revision: number;
      sequence: number;
      sessionId: null;
    };

type SessionRequestConfig = {
  _browserSessionRevision?: number;
  _retry?: boolean;
};

export class BrowserSessionChangedError extends Error {
  constructor() {
    super('Browser session changed in another tab');
    this.name = 'BrowserSessionChangedError';
  }
}

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function getBrowserSessionRevision(): number {
  return sessionVersion.revision;
}

export function clearLegacyTokenStorage() {
  if (typeof window !== 'undefined') {
    try {
      localStorage.removeItem('map_access_token');
      localStorage.removeItem('map_refresh_token');
    } catch {
      // Storage can be unavailable in hardened/private browser contexts.
    }
  }
}

export function clearTokens() {
  accessToken = null;
}

function isValidVersion(value: Partial<BrowserSessionVersion>): value is BrowserSessionVersion {
  return (
    Number.isSafeInteger(value.revision)
    && (value.revision ?? -1) >= 0
    && Number.isSafeInteger(value.sequence)
    && (value.sequence ?? -1) >= 0
  );
}

function isNewerVersion(value: BrowserSessionVersion): boolean {
  return (
    value.revision > sessionVersion.revision
    || (
      value.revision === sessionVersion.revision
      && value.sequence > sessionVersion.sequence
    )
  );
}

function isValidAccessToken(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 8192;
}

function isValidBrowserSessionId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{16,128}$/.test(value);
}

function applyActiveSession(access: string, version: BrowserSessionVersion) {
  accessToken = access;
  sessionVersion = version;
}

function applyClearedSession(version: BrowserSessionVersion) {
  clearTokens();
  sessionVersion = version;
}

function handleStoredSessionVersion(version: BrowserSessionVersion) {
  if (!isNewerVersion(version)) return;
  const sessionWasReplaced = (
    version.revision > sessionVersion.revision
    || (
      version.state === 'active'
      && sessionVersion.state === 'active'
      && version.sessionId !== sessionVersion.sessionId
    )
  );
  if (version.state === 'cleared') {
    clearAllBillingAttempts();
    applyClearedSession(version);
    notifyAuthExpired();
  } else if (version.state === 'active') {
    if (sessionWasReplaced) {
      clearAllBillingAttempts();
      clearTokens();
    }
    sessionVersion = version;
    if (sessionWasReplaced) notifyAuthReplaced();
  }
}

function assertSessionRevision(
  expectedRevision: number,
  guard?: BrowserSessionLockGuard
) {
  guard?.assertOwned();
  const previousVersion = sessionVersion;
  const sharedVersion = requireBrowserSessionVersion();
  const activeIdentityChanged = (
    previousVersion.state === 'active'
    && sharedVersion.state === 'active'
    && previousVersion.sessionId !== sharedVersion.sessionId
  );
  if (isNewerVersion(sharedVersion)) handleStoredSessionVersion(sharedVersion);
  if (
    sessionVersion.revision !== expectedRevision
    || sharedVersion.revision !== expectedRevision
    || activeIdentityChanged
  ) {
    throw new BrowserSessionChangedError();
  }
  guard?.assertOwned();
}

function ensureStorageSessionSignals() {
  if (storageSessionSignalsReady || typeof window === 'undefined') return;
  storageSessionSignalsReady = true;
  subscribeToBrowserSessionVersion(handleStoredSessionVersion);
}

function getAuthChannel(): BroadcastChannel | null {
  // BroadcastChannel is low-latency, while the storage event is a durable
  // fallback when a backgrounded tab misses a channel message.
  ensureStorageSessionSignals();
  if (
    authChannelUnavailable
    || typeof window === 'undefined'
    || typeof BroadcastChannel === 'undefined'
  ) {
    return null;
  }
  if (!authChannel) {
    try {
      authChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    } catch {
      authChannelUnavailable = true;
      return null;
    }
    authChannel.onmessage = (event: MessageEvent<AuthChannelMessage>) => {
      const message = event.data;
      if (
        !message
        || !isValidVersion(message)
        || (message.type !== 'active' && message.type !== 'cleared')
        || (
          message.type === 'active'
          && (
            !isValidAccessToken(message.access)
            || !isValidBrowserSessionId(message.sessionId)
          )
        )
        || (message.type === 'cleared' && message.sessionId !== null)
      ) return;
      const version: BrowserSessionVersion = {
        revision: message.revision,
        sequence: message.sequence,
        state: message.type === 'active' ? 'active' : 'cleared',
        sessionId: message.sessionId,
      };
      const sameActiveVersionWithoutToken = (
        message.type === 'active'
        && !accessToken
        && version.revision === sessionVersion.revision
        && version.sequence === sessionVersion.sequence
      );
      if (!isNewerVersion(version) && !sameActiveVersionWithoutToken) return;

      const sessionWasReplaced = (
        version.revision > sessionVersion.revision
        || (
          version.state === 'active'
          && sessionVersion.state === 'active'
          && version.sessionId !== sessionVersion.sessionId
        )
      );
      if (message.type === 'active') {
        if (sessionWasReplaced) clearAllBillingAttempts();
        applyActiveSession(message.access, version);
        if (sessionWasReplaced) notifyAuthReplaced();
      } else if (message.type === 'cleared') {
        clearAllBillingAttempts();
        applyClearedSession(version);
        notifyAuthExpired();
      }
    };
  }
  return authChannel;
}

function broadcastAuthMessage(message: AuthChannelMessage) {
  getAuthChannel()?.postMessage(message);
}

function publishActiveSession(
  access: string,
  replaceSession: boolean,
  sessionId: string,
  notifyReplacement = false
) {
  if (!isValidBrowserSessionId(sessionId)) {
    throw new Error('Backend did not return a browser session identifier');
  }
  const previousRevision = sessionVersion.revision;
  const version = advanceBrowserSessionVersion(
    replaceSession,
    'active',
    sessionId,
    sessionVersion
  );
  if (replaceSession || version.revision > previousRevision) {
    clearAllBillingAttempts();
  }
  applyActiveSession(access, version);
  broadcastAuthMessage({
    type: 'active',
    access,
    revision: version.revision,
    sequence: version.sequence,
    sessionId,
  });
  if (
    version.revision > previousRevision
    && (notifyReplacement || !replaceSession)
  ) {
    notifyAuthReplaced();
  }
}

function publishClearedSession() {
  const version = advanceBrowserSessionVersion(
    true,
    'cleared',
    null,
    sessionVersion
  );
  clearAllBillingAttempts();
  applyClearedSession(version);
  broadcastAuthMessage({
    type: 'cleared',
    revision: version.revision,
    sequence: version.sequence,
    sessionId: null,
  });
  notifyAuthExpired();
}

function unwrapResponse<T>(body: T | { data: T }): T {
  if (body && typeof body === 'object' && 'data' in body) {
    return body.data;
  }
  return body;
}

export async function ensureCsrfToken(force = false): Promise<string> {
  if (!force && csrfToken) return csrfToken;
  if (csrfPromise) return csrfPromise;

  csrfPromise = authTransport
    .get('/auth/browser/csrf/')
    .then(({ data }) => {
      const payload = unwrapResponse<{ csrf_token?: string }>(data);
      if (!payload.csrf_token) {
        throw new Error('Backend did not return a CSRF token');
      }
      csrfToken = payload.csrf_token;
      return csrfToken;
    })
    .finally(() => {
      csrfPromise = null;
    });

  return csrfPromise;
}

async function authPost<T>(
  path: string,
  data?: unknown,
  requestAccess?: string | null,
  guard?: BrowserSessionLockGuard
) {
  async function post(token: string) {
    guard?.assertOwned();
    const headers: Record<string, string> = { 'X-CSRFToken': token };
    if (requestAccess) {
      headers.Authorization = `Bearer ${requestAccess}`;
    }
    return authTransport.post<T>(path, data ?? {}, { headers });
  }

  try {
    return await post(await ensureCsrfToken());
  } catch (error) {
    // Recover when the browser dropped/rotated the CSRF cookie while this tab stayed open.
    if (!axios.isAxiosError(error) || error.response?.status !== 403) throw error;
    guard?.assertOwned();
    const token = await ensureCsrfToken(true);
    guard?.assertOwned();
    return post(token);
  }
}

async function performBrowserRefresh(
  expectedRevision: number,
  guard: BrowserSessionLockGuard
): Promise<string> {
  assertSessionRevision(expectedRevision, guard);
  const { data } = await authPost<
    | { access: string; browser_session_id: string }
    | { data: { access: string; browser_session_id: string } }
  >(
    '/auth/browser/refresh/',
    undefined,
    undefined,
    guard
  );
  assertSessionRevision(expectedRevision, guard);
  const payload = unwrapResponse<{
    access?: string;
    browser_session_id?: string;
  }>(data);
  if (
    !isValidAccessToken(payload.access)
    || !isValidBrowserSessionId(payload.browser_session_id)
  ) {
    throw new Error('Backend did not return complete browser session credentials');
  }
  const sessionWasReplaced = (
    sessionVersion.state !== 'active'
    || sessionVersion.sessionId !== payload.browser_session_id
  );
  publishActiveSession(
    payload.access,
    sessionWasReplaced,
    payload.browser_session_id,
    sessionWasReplaced
  );
  if (sessionWasReplaced) {
    // Never replay a request created by the previous principal with the access
    // token recovered from a different refresh-cookie chain.
    throw new BrowserSessionChangedError();
  }
  return payload.access;
}

export async function refreshBrowserSession(
  expectedRevision = sessionVersion.revision
): Promise<string> {
  if (refreshPromise) {
    const token = await refreshPromise;
    assertSessionRevision(expectedRevision);
    return token;
  }

  getAuthChannel();
  const pendingRefresh = withBrowserSessionLock(
    (guard) => performBrowserRefresh(expectedRevision, guard)
  );
  refreshPromise = pendingRefresh;
  try {
    const token = await pendingRefresh;
    assertSessionRevision(expectedRevision);
    return token;
  } catch (error) {
    if (isAuthenticationRejection(error)) {
      await expireBrowserSession(expectedRevision);
    }
    throw error;
  } finally {
    if (refreshPromise === pendingRefresh) refreshPromise = null;
  }
}

async function expireBrowserSession(expectedRevision: number): Promise<void> {
  getAuthChannel();
  try {
    await withBrowserSessionLock(async (guard) => {
      try {
        assertSessionRevision(expectedRevision, guard);
      } catch (error) {
        if (error instanceof BrowserSessionChangedError) return;
        throw error;
      }
      try {
        // The refresh credential is already known to be unusable. This call is
        // best-effort and exists to expire the stale HttpOnly cookie as well.
        await authPost(
          '/auth/browser/logout/',
          undefined,
          undefined,
          guard
        );
      } catch {
        guard.assertOwned();
      }
      assertSessionRevision(expectedRevision, guard);
      publishClearedSession();
    });
  } catch (error) {
    if (error instanceof BrowserSessionChangedError) return;
    // Shared storage failures make coordinated mutation impossible, but this
    // tab must still stop using a refresh credential proven invalid.
    clearTokens();
    notifyAuthExpired();
  }
}

function notifyAuthExpired() {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
}

function notifyAuthReplaced() {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(AUTH_REPLACED_EVENT));
  }
}

export function isAuthenticationRejection(error: unknown): boolean {
  return axios.isAxiosError(error) && error.response?.status === 401;
}

export function subscribeToAuthExpired(listener: () => void) {
  if (typeof window === 'undefined') return () => undefined;
  getAuthChannel();
  window.addEventListener(AUTH_EXPIRED_EVENT, listener);
  return () => window.removeEventListener(AUTH_EXPIRED_EVENT, listener);
}

export function subscribeToAuthReplaced(listener: () => void) {
  if (typeof window === 'undefined') return () => undefined;
  getAuthChannel();
  window.addEventListener(AUTH_REPLACED_EVENT, listener);
  return () => window.removeEventListener(AUTH_REPLACED_EVENT, listener);
}

// === Interceptors ===

// Capture the browser-session revision synchronously with the token used by the
// original request. A later retry must remain in that same logical session.
api.interceptors.request.use(
  (config) => {
    const sessionConfig = config as typeof config & SessionRequestConfig;
    const expectedRevision = sessionConfig._browserSessionRevision
      ?? sessionVersion.revision;
    assertSessionRevision(expectedRevision);
    sessionConfig._browserSessionRevision = expectedRevision;
    const token = getAccessToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    throw error;
  },
  { synchronous: true }
);

api.interceptors.response.use(
  (response) => {
    const responseConfig = response.config as typeof response.config & SessionRequestConfig;
    const requestRevision = responseConfig._browserSessionRevision;
    if (Number.isSafeInteger(requestRevision)) {
      assertSessionRevision(requestRevision as number);
    }
    return response;
  },
  async (error) => {
    const originalRequest = error.config;
    const requestRevision = (
      originalRequest as (typeof originalRequest & SessionRequestConfig) | undefined
    )?._browserSessionRevision;
    if (Number.isSafeInteger(requestRevision)) {
      try {
        assertSessionRevision(requestRevision as number);
      } catch (sessionError) {
        return Promise.reject(sessionError);
      }
    }

    if (
      error.response?.status === 402
      && error.response?.data?.code === 'subscription_inactive'
      && typeof window !== 'undefined'
      && !window.location.pathname.startsWith('/dashboard/billing')
    ) {
      window.location.replace('/dashboard/billing?subscription=inactive');
      return Promise.reject(error);
    }

    if (error.response?.status === 401 && originalRequest && !originalRequest._retry) {
      const sessionRequest = originalRequest as typeof originalRequest & SessionRequestConfig;
      const retryRevision = sessionRequest._browserSessionRevision;
      if (!Number.isSafeInteger(retryRevision)) return Promise.reject(error);
      sessionRequest._retry = true;

      try {
        assertSessionRevision(retryRevision as number);
        const token = await refreshBrowserSession(retryRevision as number);
        assertSessionRevision(retryRevision as number);
        if (getAccessToken() !== token) throw new BrowserSessionChangedError();
        originalRequest.headers.Authorization = `Bearer ${token}`;
        return api(originalRequest);
      } catch (refreshError) {
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

export default api;

// === API functions ===

// Auth
export const authApi = {
  login: async (email: string, password: string, tenant_slug?: string) => {
    getAuthChannel();
    return withBrowserSessionLock(async (guard) => {
      guard.assertOwned();
      const response = await authPost<
        | { access: string; browser_session_id: string }
        | { data: { access: string; browser_session_id: string } }
      >(
        '/auth/browser/login/',
        { email, password, tenant_slug },
        undefined,
        guard
      );
      guard.assertOwned();
      const payload = unwrapResponse<{
        access?: string;
        browser_session_id?: string;
      }>(response.data);
      if (
        !isValidAccessToken(payload.access)
        || !isValidBrowserSessionId(payload.browser_session_id)
      ) {
        throw new Error('Backend did not return complete browser session credentials');
      }
      publishActiveSession(payload.access, true, payload.browser_session_id);
      return Object.assign(response, {
        browserSessionRevision: sessionVersion.revision,
      });
    });
  },
  refresh: refreshBrowserSession,
  logout: async () => {
    const expectedRevision = sessionVersion.revision;
    const requestAccess = accessToken;
    getAuthChannel();
    return withBrowserSessionLock(async (guard) => {
      assertSessionRevision(expectedRevision, guard);
      const response = await authPost(
        '/auth/browser/logout/',
        undefined,
        requestAccess,
        guard
      );
      assertSessionRevision(expectedRevision, guard);
      publishClearedSession();
      return response;
    });
  },
  logoutAll: async () => {
    const expectedRevision = sessionVersion.revision;
    const requestAccess = accessToken;
    getAuthChannel();
    return withBrowserSessionLock(async (guard) => {
      assertSessionRevision(expectedRevision, guard);
      const response = await authPost(
        '/auth/browser/logout-all/',
        undefined,
        requestAccess,
        guard
      );
      assertSessionRevision(expectedRevision, guard);
      publishClearedSession();
      return response;
    });
  },
  register: (data: { name: string; slug: string; email: string; password: string }) =>
    authPost('/auth/register/', data),
  forgotPassword: (email: string) =>
    authPost('/auth/password-reset/', { email }),
  resetPassword: (data: { uid: string; token: string; new_password: string }) =>
    authPost('/auth/password-reset/confirm/', data),
  me: () => api.get('/auth/me/'),
};

// Profile
export const profileApi = {
  updatePhone: (phone: string) => api.patch('/auth/profile/', { phone }),
  changePassword: (current_password: string, new_password: string) => {
    const expectedRevision = sessionVersion.revision;
    const requestAccess = accessToken;
    if (!requestAccess) throw new BrowserSessionChangedError();
    return withBrowserSessionLock(async (guard) => {
      assertSessionRevision(expectedRevision, guard);
      const response = await authPost(
        '/auth/change-password/',
        { current_password, new_password },
        requestAccess,
        guard
      );
      assertSessionRevision(expectedRevision, guard);
      publishClearedSession();
      return response;
    });
  },
  changeEmail: (new_email: string, current_password: string) =>
    api.post('/auth/change-email/', { new_email, current_password }),
  confirmEmail: (token: string) => authPost('/auth/confirm-email/', { token }),
};

// Tenant
export const tenantApi = {
  get: () => api.get('/tenant/'),
  catalogDomains: () => api.get('/catalog-domains/'),
  setCatalogDomainEnabled: (domainSlug: string, isEnabled: boolean) =>
    api.post('/catalog-domains/', { domain_slug: domainSlug, is_enabled: isEnabled }),
  getUsers: () => api.get('/tenant/users/'),
  getApiKeys: () => api.get('/tenant/api-keys/'),
  createApiKey: (name: string) => api.post('/tenant/api-keys/', { name }),
  revokeApiKey: (id: number) => api.delete(`/tenant/api-keys/${id}/`),
};

// Billing
const BILLING_ATTEMPT_PREFIX = 'map:billing-attempt:';
type BillingPaymentResponse = {
  status: string;
  data?: { payment_url?: string };
};

function createIdempotencyUuid(): string {
  if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') {
    throw new Error('Secure UUID generation is unavailable in this browser');
  }
  return crypto.randomUUID();
}

function billingAttemptStorageKey(fingerprint: string): string {
  return `${BILLING_ATTEMPT_PREFIX}${fingerprint}`;
}

function isUuid(value: string | null): value is string {
  return Boolean(value && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value));
}

async function getOrCreateBillingAttempt(
  fingerprint: string,
  expectedRevision: number
): Promise<string> {
  const storageKey = billingAttemptStorageKey(fingerprint);
  return withBrowserSessionLock(async (guard) => {
    assertSessionRevision(expectedRevision, guard);
    const storage = requireBrowserSessionStorage();
    const stored = storage.getItem(storageKey);
    if (isUuid(stored)) {
      return stored;
    }
    if (stored) storage.removeItem(storageKey);
    const idempotencyKey = createIdempotencyUuid();
    storage.setItem(storageKey, idempotencyKey);
    if (storage.getItem(storageKey) !== idempotencyKey) {
      throw new Error('Billing attempt could not be persisted');
    }
    assertSessionRevision(expectedRevision, guard);
    return idempotencyKey;
  });
}

function clearBillingAttempt(fingerprint: string) {
  const storageKey = billingAttemptStorageKey(fingerprint);
  try {
    window.localStorage.removeItem(storageKey);
    // Remove keys written by pre-hardening builds as well.
    window.sessionStorage.removeItem(storageKey);
  } catch {
    // Session replacement still clears the in-memory access credential.
  }
}

function clearAllBillingAttempts() {
  if (typeof window === 'undefined') return;
  try {
    for (let index = window.localStorage.length - 1; index >= 0; index -= 1) {
      const key = window.localStorage.key(index);
      if (key?.startsWith(BILLING_ATTEMPT_PREFIX)) {
        window.localStorage.removeItem(key);
      }
    }
  } catch {
    // Auth mutations fail closed if shared storage is unavailable.
  }
  try {
    for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(BILLING_ATTEMPT_PREFIX)) {
        window.sessionStorage.removeItem(key);
      }
    }
  } catch {
    // Legacy per-tab keys are best-effort cleanup only.
  }
}

function shouldRotateBillingAttempt(error: unknown): boolean {
  if (!axios.isAxiosError(error) || !error.response) return false;
  const payload = error.response.data;
  return (
    payload?.code === 'checkout_terminal'
    && payload?.data?.rotate_idempotency_key === true
  );
}

async function postBillingAttempt<T>(
  fingerprint: string,
  path: string,
  payload: Record<string, unknown>
) {
  const expectedRevision = sessionVersion.revision;
  const idempotencyKey = await getOrCreateBillingAttempt(
    fingerprint,
    expectedRevision
  );
  try {
    assertSessionRevision(expectedRevision);
    const requestConfig: AxiosRequestConfig & SessionRequestConfig = {
      _browserSessionRevision: expectedRevision,
      timeout: BILLING_MUTATION_TIMEOUT_MS,
    };
    return await api.post<T>(path, {
      ...payload,
      idempotency_key: idempotencyKey,
    }, requestConfig);
  } catch (error) {
    // Fail closed: a transport error or an arbitrary 4xx must keep the same
    // key. Only the backend's explicit terminal-intent signal may rotate it.
    if (shouldRotateBillingAttempt(error)) clearBillingAttempt(fingerprint);
    throw error;
  }
}

export const billingApi = {
  getPlans: () => api.get('/billing/plans/', { timeout: BILLING_READ_TIMEOUT_MS }),
  getSubscription: () => api.get('/billing/subscription/', { timeout: BILLING_READ_TIMEOUT_MS }),
  getUsage: () => api.get('/billing/usage/', { timeout: BILLING_READ_TIMEOUT_MS }),
  getInvoices: () => api.get('/billing/invoices/', { timeout: BILLING_READ_TIMEOUT_MS }),
  getAIPackages: () => api.get('/billing/ai-packages/', { timeout: BILLING_READ_TIMEOUT_MS }),
  checkout: (plan_slug: string, period: 'monthly' | 'yearly') => (
    postBillingAttempt<BillingPaymentResponse>(
      `r${sessionVersion.revision}:subscription:${plan_slug}:${period}`,
      '/billing/checkout/',
      { plan_slug, period }
    )
  ),
  topupAI: (package_id: number) => postBillingAttempt<BillingPaymentResponse>(
    `r${sessionVersion.revision}:ai-topup:${package_id}`,
    '/billing/ai-topup/',
    { package_id }
  ),
};

// AI models and tenant routing
export const aiApi = {
  getModels: () => api.get('/ai/models/'),
  getSettings: () => api.get('/ai/settings/'),
  updateSettings: (data: {
    default_model: number;
    use_task_overrides: boolean;
    task_models: Record<string, number>;
  }) => api.patch('/ai/settings/', data),
  getUsage: () => api.get('/ai/usage/'),
};

// Datasources
export const datasourceApi = {
  list: () => api.get('/datasources/'),
  get: (id: number) => api.get(`/datasources/${id}/`),
  create: (data: Record<string, unknown>) => api.post('/datasources/', data),
  update: (id: number, data: Record<string, unknown>) => api.put(`/datasources/${id}/`, data),
  delete: (id: number) => api.delete(`/datasources/${id}/`),
  test: (id: number) => api.post(`/datasources/${id}/test/`),
  sync: (id: number) => api.post(`/datasources/${id}/sync/`),
  uploadCsv: (file: File, confirm = false) => {
    const formData = new FormData();
    formData.append('file', file);
    const url = confirm ? '/datasources/upload-csv/?confirm=1' : '/datasources/upload-csv/';
    return api.post(url, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
};

// Products
export const productApi = {
  list: (params?: Record<string, unknown>) => api.get('/products/', { params }),
  get: (id: number) => api.get(`/products/${id}/`),
  updateBrand: (id: number, brand: string) => api.patch(`/products/${id}/`, { brand }),
  brandOptions: (productId: number, q = '') =>
    api.get('/products/brand-options/', { params: { product_id: productId, q } }),
  publish: (id: number) => api.post(`/products/${id}/publish/`),
  archive: (id: number) => api.post(`/products/${id}/archive/`),
  regenerate: (id: number) => api.post(`/products/${id}/regenerate/`),
  parse: (id: number, source = '', generateAfter = false) =>
    api.post('/products/parse/', { product_id: id, source: source || undefined, generate_after: generateAfter }),
  parseJobStatus: (id: number) => api.get(`/products/parse-jobs/${id}/`),
  startWebResearch: (id: number, generateAfter = false) =>
    api.post(`/products/${id}/web-research/`, { generate_after: generateAfter }),
  latestWebResearch: (id: number) => api.get(`/products/${id}/web-research/`),
  webResearchStatus: (runId: number) => api.get(`/web-research/runs/${runId}/`),
  bulkAction: (data: {
    action: string;
    product_ids: number[];
    source?: string;
    batch_size?: number;
    pause_seconds?: number;
  }) => api.post('/products/bulk-actions/', data),
  bulkActionStatus: (id: number) => api.get(`/products/bulk-actions/${id}/`),
  catalogCategories: (params?: { assignable?: boolean }) =>
    api.get('/products/catalog-categories/', { params }),
  createCatalogCategory: (data: Record<string, unknown>) =>
    api.post('/products/catalog-categories/', data),
  updateCatalogCategory: (id: number, data: Record<string, unknown>) =>
    api.put(`/products/catalog-categories/${id}/`, data),
  patchCatalogCategory: (id: number, data: Record<string, unknown>) =>
    api.patch(`/products/catalog-categories/${id}/`, data),
  deleteCatalogCategory: (id: number, hard = false) =>
    api.delete(`/products/catalog-categories/${id}/${hard ? '?hard=true' : ''}`),
  toggleCatalogCategoryBranch: (id: number, isActive: boolean) =>
    api.post(`/products/catalog-categories/${id}/toggle-branch/`, { is_active: isActive }),
  assignCatalogCategory: (data: { product_ids: number[]; catalog_category: number | null }) =>
    api.post('/products/catalog-categories/assign/', data),
  excludeFromSync: (product_ids: number[], exclude: boolean) =>
    api.post('/products/exclude/', { product_ids, exclude }),
  bulkDelete: (product_ids: number[]) =>
    api.delete('/products/bulk-delete/', { data: { product_ids } }),
  reviewCatalogClassification: (productId: number, action: 'approve' | 'reject') =>
    api.post(`/products/${productId}/catalog-classification/${action}/`),
  reviewFitment: (productId: number, fitmentId: number, action: 'approve' | 'reject') =>
    api.post(`/products/${productId}/fitments/${fitmentId}/${action}/`),
  reviewEnrichmentFact: (productId: number, factId: number, action: 'approve' | 'reject') =>
    api.post(`/products/${productId}/enrichment-facts/${factId}/${action}/`),
  reviewQueue: (params?: Record<string, unknown>) => api.get('/products/review-queue/', { params }),
  reviewQueueAction: (type: string, recordId: number, action: 'approve' | 'reject') =>
    api.post(`/products/review-queue/${type}/${recordId}/${action}/`),
  uploadCatalogCategoryImage: (id: number, file: File) => {
    const formData = new FormData();
    formData.append('image', file);
    return api.post(`/products/catalog-categories/${id}/default-image/`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  deleteCatalogCategoryImage: (id: number) =>
    api.delete(`/products/catalog-categories/${id}/default-image/`),
  catalogCategoryMappings: () => api.get('/products/catalog-category-mappings/'),
  createCatalogCategoryMapping: (data: Record<string, unknown>) =>
    api.post('/products/catalog-category-mappings/', data),
  deleteCatalogCategoryMapping: (id: number) =>
    api.delete(`/products/catalog-category-mappings/${id}/`),
  catalogSourceCategories: () => api.get('/products/catalog-source-categories/'),
};

export const webResearchApi = {
  list: (params?: Record<string, unknown>) => api.get('/web-research/runs/', { params }),
  get: (runId: number) => api.get(`/web-research/runs/${runId}/`),
  providers: () => api.get('/web-research/providers/'),
  settings: () => api.get('/web-research/settings/'),
  updateSettings: (data: object) => api.put('/web-research/settings/', data),
  startMarketResearch: (productId: number, force = false) =>
    api.post(`/products/${productId}/market-research/`, { force }),
  marketOffers: (productId: number, params?: Record<string, unknown>) =>
    api.get(`/products/${productId}/market-offers/`, { params }),
};

// Marketplace Accounts
export const accountApi = {
  list: () => api.get('/accounts/'),
  create: (data: Record<string, unknown>) => api.post('/accounts/', data),
  patch: (id: number, data: Record<string, unknown>) => api.patch(`/accounts/${id}/`, data),
  delete: (id: number) => api.delete(`/accounts/${id}/`),
  checkAutoload: (id: number) => api.get(`/accounts/${id}/autoload-status/`),
  listPlacementAddresses: (params?: Record<string, unknown>) =>
    api.get('/accounts/placement-addresses/', { params }),
  createPlacementAddress: (data: Record<string, unknown>) =>
    api.post('/accounts/placement-addresses/', data),
  patchPlacementAddress: (id: number, data: Record<string, unknown>) =>
    api.patch(`/accounts/placement-addresses/${id}/`, data),
  deletePlacementAddress: (id: number) =>
    api.delete(`/accounts/placement-addresses/${id}/`),
};

// Categories
export const categoryApi = {
  getMappings: () => api.get('/categories/mappings/'),
  getUnmapped: () => api.get('/categories/unmapped/'),
  createMapping: (data: Record<string, unknown>) => api.post('/categories/mappings/', data),
  updateMapping: (id: number, data: Record<string, unknown>) =>
    api.put(`/categories/mappings/${id}/`, data),
};

// Listings
export const listingApi = {
  list: (params?: Record<string, unknown>) => api.get('/listings/', { params }),
  get: (id: number) => api.get(`/listings/${id}/`),
  marketComparison: (id: number) => api.get(`/listings/${id}/market-comparison/`),
  approve: (id: number) => api.post(`/listings/${id}/approve/`),
  refreshBrandCatalog: (id: number) => api.post(`/listings/${id}/refresh-brand-catalog/`),
  publish: (id: number) => api.post(`/listings/${id}/publish/`),
  archive: (id: number) => api.post(`/listings/${id}/archive/`),
  delete: (id: number) => api.post(`/listings/${id}/delete/`),
  checkStatus: (id: number) => api.post(`/listings/${id}/check-status/`),
  regenerate: (id: number) => api.post(`/listings/${id}/regenerate/`),
  updateContent: (id: number, data: {
    title?: string;
    description_ai?: string;
    address_override?: string;
    seller_address_id_override?: string;
    manager_name_override?: string;
    contact_phone_override?: string;
    placement_address?: number | null;
    account_id?: number;
    price_on_listing?: string;
    margin_pct?: string | null;
    ad_type?: string;
  }) =>
    api.patch(`/listings/${id}/`, data),
  bulkPlacement: (data: Record<string, unknown>) =>
    api.post('/listings/bulk-placement/', data),
  bulkAction: (data: Record<string, unknown>) =>
    api.post('/listings/bulk-actions/', data),
};

// Logs
export const logApi = {
  list: (params?: Record<string, unknown>) => api.get('/logs/', { params }),
};

// Analytics
export const analyticsApi = {
  get: (params?: Record<string, unknown>) => api.get('/analytics/', { params }),
};

// Notifications
export const notificationApi = {
  getSettings: () => api.get('/notifications/settings/'),
  updateSettings: (data: Record<string, unknown>) => api.put('/notifications/settings/', data),
  telegramConnect: () => api.post('/notifications/settings/telegram/connect/'),
  telegramDisconnect: () => api.delete('/notifications/settings/telegram/'),
  test: () => api.post('/notifications/settings/test/'),
};

// Images
export const imageApi = {
  list: (productId: number) =>
    api.get(`/products/${productId}/images/`),
  search: (productId: number) =>
    api.post(`/products/${productId}/images/search/`),
  searchStatus: (productId: number, taskId: string) =>
    api.get(`/products/${productId}/images/search/${taskId}/`),
  approve: (productId: number, imageId: number) =>
    api.post(`/products/${productId}/images/${imageId}/approve/`),
  reject: (productId: number, imageId: number) =>
    api.post(`/products/${productId}/images/${imageId}/reject/`),
  setPrimary: (productId: number, imageId: number) =>
    api.put(`/products/${productId}/images/${imageId}/set_primary/`),
  delete: (productId: number, imageId: number) =>
    api.delete(`/products/${productId}/images/${imageId}/`),
  upload: (productId: number, file: File) => {
    const formData = new FormData();
    formData.append('image', file);
    return api.post(`/products/${productId}/images/upload/`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  bulkSearch: (productIds: number[]) =>
    api.post('/images/bulk-search/', { product_ids: productIds }),
  getQuota: () => api.get('/images/quota/'),
};

// Provider-agnostic media processing
export const mediaApi = {
  providers: () => api.get('/media/providers/'),
  presets: () => api.get('/media/presets/'),
  createPreset: (data: Record<string, unknown>) => api.post('/media/presets/', data),
  settings: () => api.get('/media/settings/'),
  updateSettings: (data: Record<string, unknown>) => api.patch('/media/settings/', data),
  jobs: () => api.get('/media/jobs/'),
  job: (id: number) => api.get(`/media/jobs/${id}/`),
  assessments: () => api.get('/media/assessments/'),
  process: (
    productId: number,
    imageId: number,
    data: {
      preset_id?: number;
      operations?: string[];
      parameters?: Record<string, unknown>;
      provider_id?: string;
      idempotency_key?: string;
    },
  ) => api.post(`/products/${productId}/images/${imageId}/process/`, data),
  activateVariant: (variantId: number) =>
    api.post(`/media/variants/${variantId}/activate/`),
};

// Webhooks
export const webhookApi = {
  list: () => api.get('/webhooks/'),
  events: () => api.get('/webhooks/events/'),
  create: (data: { url: string; events: string[] }) => api.post('/webhooks/', data),
  delete: (id: number) => api.delete(`/webhooks/${id}/`),
  test: (id: number) => api.post(`/webhooks/${id}/test/`),
};
