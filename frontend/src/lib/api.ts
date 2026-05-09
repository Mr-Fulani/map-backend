/**
 * API-клиент для взаимодействия с Django backend.
 * Автоматически обновляет JWT access token через refresh при 401.
 */

import axios from 'axios';

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

const api = axios.create({
  baseURL: `${API_BASE_URL}/api/v1`,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
});

// === Token management ===

let accessToken: string | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
  if (token) {
    localStorage.setItem('map_access_token', token);
  } else {
    localStorage.removeItem('map_access_token');
  }
}

export function getAccessToken(): string | null {
  if (accessToken) return accessToken;
  if (typeof window !== 'undefined') {
    accessToken = localStorage.getItem('map_access_token');
  }
  return accessToken;
}

export function setRefreshToken(token: string | null) {
  if (token) {
    localStorage.setItem('map_refresh_token', token);
  } else {
    localStorage.removeItem('map_refresh_token');
  }
}

export function getRefreshToken(): string | null {
  if (typeof window !== 'undefined') {
    return localStorage.getItem('map_refresh_token');
  }
  return null;
}

export function clearTokens() {
  accessToken = null;
  localStorage.removeItem('map_access_token');
  localStorage.removeItem('map_refresh_token');
}

// === Interceptors ===

// Request: добавляем Authorization header
api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Response: при 401 пробуем refresh
let isRefreshing = false;
let failedQueue: Array<{
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
}> = [];

const processQueue = (error: unknown, token: string | null = null) => {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error);
    } else {
      prom.resolve(token);
    }
  });
  failedQueue = [];
};

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;

    if (error.response?.status === 401 && !originalRequest._retry) {
      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        })
          .then((token) => {
            originalRequest.headers.Authorization = `Bearer ${token}`;
            return api(originalRequest);
          })
          .catch((err) => Promise.reject(err));
      }

      originalRequest._retry = true;
      isRefreshing = true;

      const refreshToken = getRefreshToken();
      if (!refreshToken) {
        clearTokens();
        window.location.href = '/login';
        return Promise.reject(error);
      }

      try {
        const { data } = await axios.post(`${API_BASE_URL}/api/v1/auth/token/refresh/`, {
          refresh: refreshToken,
        });

        setAccessToken(data.access);
        if (data.refresh) {
          setRefreshToken(data.refresh);
        }

        processQueue(null, data.access);
        originalRequest.headers.Authorization = `Bearer ${data.access}`;
        return api(originalRequest);
      } catch (refreshError) {
        processQueue(refreshError, null);
        clearTokens();
        window.location.href = '/login';
        return Promise.reject(refreshError);
      } finally {
        isRefreshing = false;
      }
    }

    return Promise.reject(error);
  }
);

export default api;

// === API functions ===

// Auth
export const authApi = {
  login: (email: string, password: string, tenant_slug?: string) =>
    api.post('/auth/token/', { email, password, tenant_slug }),
  refresh: (refresh: string) =>
    api.post('/auth/token/refresh/', { refresh }),
  register: (data: { name: string; slug: string; email: string; password: string }) =>
    api.post('/auth/register/', data),
  me: () => api.get('/auth/me/'),
};

// Profile
export const profileApi = {
  updatePhone: (phone: string) => api.patch('/auth/profile/', { phone }),
  changePassword: (current_password: string, new_password: string) =>
    api.post('/auth/change-password/', { current_password, new_password }),
  changeEmail: (new_email: string) => api.post('/auth/change-email/', { new_email }),
  confirmEmail: (token: string) => api.get('/auth/confirm-email/', { params: { token } }),
};

// Tenant
export const tenantApi = {
  get: () => api.get('/tenant/'),
  getUsers: () => api.get('/tenant/users/'),
  getApiKeys: () => api.get('/tenant/api-keys/'),
  createApiKey: (name: string) => api.post('/tenant/api-keys/', { name }),
  revokeApiKey: (id: number) => api.delete(`/tenant/api-keys/${id}/`),
};

// Billing
export const billingApi = {
  getPlans: () => api.get('/billing/plans/'),
  getSubscription: () => api.get('/billing/subscription/'),
  getUsage: () => api.get('/billing/usage/'),
  getInvoices: () => api.get('/billing/invoices/'),
  checkout: (plan_slug: string, period: string) =>
    api.post('/billing/checkout/', { plan_slug, period }),
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
  uploadCsv: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post('/datasources/upload-csv/', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
};

// Products
export const productApi = {
  list: (params?: Record<string, unknown>) => api.get('/products/', { params }),
  get: (id: number) => api.get(`/products/${id}/`),
  publish: (id: number) => api.post(`/products/${id}/publish/`),
  archive: (id: number) => api.post(`/products/${id}/archive/`),
  regenerate: (id: number) => api.post(`/products/${id}/regenerate/`),
};

// Marketplace Accounts
export const accountApi = {
  list: () => api.get('/accounts/'),
  create: (data: Record<string, unknown>) => api.post('/accounts/', data),
  delete: (id: number) => api.delete(`/accounts/${id}/`),
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
  approve: (id: number) => api.post(`/listings/${id}/approve/`),
  regenerate: (id: number) => api.post(`/listings/${id}/regenerate/`),
  updateContent: (id: number, data: { title?: string; description_ai?: string }) =>
    api.patch(`/listings/${id}/`, data),
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

// Webhooks
export const webhookApi = {
  list: () => api.get('/webhooks/'),
  events: () => api.get('/webhooks/events/'),
  create: (data: { url: string; events: string[] }) => api.post('/webhooks/', data),
  delete: (id: number) => api.delete(`/webhooks/${id}/`),
  test: (id: number) => api.post(`/webhooks/${id}/test/`),
};
