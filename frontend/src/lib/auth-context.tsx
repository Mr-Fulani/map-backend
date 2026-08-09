/**
 * Auth context — управление состоянием аутентификации.
 */

'use client';

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  authApi,
  clearLegacyTokenStorage,
  clearTokens,
  ensureCsrfToken,
  getAccessToken,
  getBrowserSessionRevision,
  isAuthenticationRejection,
  refreshBrowserSession,
  subscribeToAuthExpired,
  subscribeToAuthReplaced,
} from '@/lib/api';

interface User {
  id: number;
  email: string;
  phone?: string;
}

interface Tenant {
  id: number;
  slug: string;
  name: string;
  catalog_domain?: string;
}

interface Subscription {
  plan_slug: string;
  plan_name: string;
  status: string;
  access_mode: 'full' | 'billing_only';
  current_period_end: string | null;
}

interface AuthState {
  user: User | null;
  tenant: Tenant | null;
  role: string | null;
  subscription: Subscription | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  hasAuthError: boolean;
}

interface AuthContextType extends AuthState {
  login: (email: string, password: string, tenantSlug?: string) => Promise<void>;
  logout: () => Promise<void>;
  logoutAll: () => Promise<void>;
  clearLocalSession: () => void;
  refreshMe: () => Promise<void>;
  retrySession: () => Promise<void>;
}

interface AuthOperation {
  generation: number;
  revision: number;
}

const AuthContext = createContext<AuthContextType | null>(null);

const signedOutState: AuthState = {
  user: null,
  tenant: null,
  role: null,
  subscription: null,
  isLoading: false,
  isAuthenticated: false,
  hasAuthError: false,
};

function unwrapData<T>(body: T | { data: T }): T {
  if (body && typeof body === 'object' && 'data' in body) {
    return body.data;
  }
  return body;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    tenant: null,
    role: null,
    subscription: null,
    isLoading: true,
    isAuthenticated: false,
    hasAuthError: false,
  });

  const operationGeneration = useRef(0);
  const beginAuthOperation = useCallback((): AuthOperation => ({
    generation: ++operationGeneration.current,
    revision: getBrowserSessionRevision(),
  }), []);
  const isCurrentAuthOperation = useCallback((operation: AuthOperation) => (
    operation.generation === operationGeneration.current
    && operation.revision === getBrowserSessionRevision()
  ), []);
  const invalidateAuthOperations = useCallback(() => {
    operationGeneration.current += 1;
  }, []);

  const refreshMe = useCallback(async (operation?: AuthOperation) => {
    const activeOperation = operation ?? beginAuthOperation();
    try {
      if (!getAccessToken()) {
        if (isCurrentAuthOperation(activeOperation)) setState(signedOutState);
        return;
      }

      const { data } = await authApi.me();
      const payload = unwrapData<{
        user: User;
        tenant: Tenant;
        role: string;
        subscription: Subscription | null;
      }>(data);
      if (!isCurrentAuthOperation(activeOperation)) return;
      setState({
        user: payload.user,
        tenant: payload.tenant,
        role: payload.role,
        subscription: payload.subscription,
        isLoading: false,
        isAuthenticated: true,
        hasAuthError: false,
      });
    } catch (error) {
      if (!isCurrentAuthOperation(activeOperation)) return;
      if (isAuthenticationRejection(error)) {
        clearTokens();
        setState(signedOutState);
      } else {
        setState((current) => ({
          ...current,
          isLoading: false,
          hasAuthError: true,
        }));
      }
      throw error;
    }
  }, [beginAuthOperation, isCurrentAuthOperation]);

  const login = useCallback(
    async (email: string, password: string, tenantSlug?: string) => {
      const response = await authApi.login(email, password, tenantSlug);
      const { data } = response;
      const payload = unwrapData<{
        access?: string;
        user?: User;
        tenant?: Tenant;
        role?: string;
      }>(data);
      if (!payload.access || !payload.user || !payload.tenant || !payload.role) {
        throw new Error('Backend did not return an access token');
      }
      if (
        response.browserSessionRevision !== getBrowserSessionRevision()
        || !getAccessToken()
      ) {
        throw new Error('Browser session changed in another tab');
      }
      const operation = beginAuthOperation();
      if (
        operation.revision !== response.browserSessionRevision
        || !isCurrentAuthOperation(operation)
      ) {
        throw new Error('Browser session changed in another tab');
      }
      setState({
        user: payload.user,
        tenant: payload.tenant,
        role: payload.role,
        subscription: null,
        isLoading: true,
        isAuthenticated: true,
        hasAuthError: false,
      });
      try {
        await refreshMe(operation);
      } catch (error) {
        if (isAuthenticationRejection(error)) throw error;
        // Login itself succeeded. Keep the authenticated summary and allow the
        // next request/reload to recover transient /me failures.
      }
    },
    [beginAuthOperation, isCurrentAuthOperation, refreshMe]
  );

  const clearLocalSession = useCallback(() => {
    invalidateAuthOperations();
    clearTokens();
    setState(signedOutState);
  }, [invalidateAuthOperations]);

  const logout = useCallback(async () => {
    await authApi.logout();
    clearLocalSession();
  }, [clearLocalSession]);

  const logoutAll = useCallback(async () => {
    await authApi.logoutAll();
    clearLocalSession();
  }, [clearLocalSession]);

  const retrySession = useCallback(async () => {
    const operation = beginAuthOperation();
    if (isCurrentAuthOperation(operation)) {
      setState((current) => ({
        ...current,
        isLoading: true,
        hasAuthError: false,
      }));
    }
    try {
      await ensureCsrfToken();
      if (!getAccessToken()) await refreshBrowserSession();
      if (!isCurrentAuthOperation(operation)) return;
      await refreshMe(operation);
    } catch (error) {
      if (!isCurrentAuthOperation(operation)) return;
      if (isAuthenticationRejection(error)) {
        clearTokens();
        setState(signedOutState);
      } else {
        setState((current) => ({
          ...current,
          isLoading: false,
          hasAuthError: true,
        }));
      }
      throw error;
    }
  }, [beginAuthOperation, isCurrentAuthOperation, refreshMe]);

  // Bootstrap is deliberately ordered: CSRF cookie/token → refresh cookie → /me.
  useEffect(() => {
    let active = true;
    const operation = beginAuthOperation();
    clearLegacyTokenStorage();

    async function bootstrap() {
      try {
        await ensureCsrfToken();
        if (!getAccessToken()) await refreshBrowserSession();
        if (active && isCurrentAuthOperation(operation)) {
          await refreshMe(operation);
        }
      } catch (error) {
        if (active && isCurrentAuthOperation(operation)) {
          if (isAuthenticationRejection(error)) clearTokens();
          setState((current) => (
            isAuthenticationRejection(error)
              ? signedOutState
              : { ...current, isLoading: false, hasAuthError: true }
          ));
        }
      }
    }

    void bootstrap();
    return () => {
      active = false;
    };
  }, [beginAuthOperation, isCurrentAuthOperation, refreshMe]);

  useEffect(
    () => subscribeToAuthExpired(() => {
      invalidateAuthOperations();
      setState(signedOutState);
    }),
    [invalidateAuthOperations]
  );

  useEffect(
    () => subscribeToAuthReplaced(() => {
      const operation = beginAuthOperation();
      setState({ ...signedOutState, isLoading: true });
      void (async () => {
        if (!getAccessToken()) await refreshBrowserSession();
        if (!isCurrentAuthOperation(operation)) return;
        await refreshMe(operation);
      })().catch((error) => {
        if (!isCurrentAuthOperation(operation)) return;
        if (isAuthenticationRejection(error)) {
          clearTokens();
          setState(signedOutState);
        } else {
          setState((current) => ({
            ...current,
            isLoading: false,
            hasAuthError: true,
          }));
        }
      });
    }),
    [beginAuthOperation, isCurrentAuthOperation, refreshMe]
  );

  return (
    <AuthContext.Provider
      value={{
        ...state,
        login,
        logout,
        logoutAll,
        clearLocalSession,
        refreshMe,
        retrySession,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
