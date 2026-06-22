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
  catalogDomains: () => api.get('/catalog-domains/'),
  setCatalogDomainEnabled: (domainSlug: string, isEnabled: boolean) =>
    api.post('/catalog-domains/', { domain_slug: domainSlug, is_enabled: isEnabled }),
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
  parse: (id: number, source = '', generateAfter = false) =>
    api.post('/products/parse/', { product_id: id, source: source || undefined, generate_after: generateAfter }),
  parseJobStatus: (id: number) => api.get(`/products/parse-jobs/${id}/`),
  bulkAction: (data: {
    action: string;
    product_ids: number[];
    source?: string;
    batch_size?: number;
    pause_seconds?: number;
  }) => api.post('/products/bulk-actions/', data),
  bulkActionStatus: (id: number) => api.get(`/products/bulk-actions/${id}/`),
  catalogCategories: () => api.get('/products/catalog-categories/'),
  createCatalogCategory: (data: Record<string, unknown>) =>
    api.post('/products/catalog-categories/', data),
  updateCatalogCategory: (id: number, data: Record<string, unknown>) =>
    api.put(`/products/catalog-categories/${id}/`, data),
  deleteCatalogCategory: (id: number) => api.delete(`/products/catalog-categories/${id}/`),
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
  approve: (id: number) => api.post(`/listings/${id}/approve/`),
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

// Webhooks
export const webhookApi = {
  list: () => api.get('/webhooks/'),
  events: () => api.get('/webhooks/events/'),
  create: (data: { url: string; events: string[] }) => api.post('/webhooks/', data),
  delete: (id: number) => api.delete(`/webhooks/${id}/`),
  test: (id: number) => api.post(`/webhooks/${id}/test/`),
};
