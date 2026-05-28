/**
 * Auth context — управление состоянием аутентификации.
 */

'use client';

import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import { authApi, setAccessToken, setRefreshToken, clearTokens, getAccessToken } from '@/lib/api';

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
  current_period_end: string | null;
}

interface AuthState {
  user: User | null;
  tenant: Tenant | null;
  role: string | null;
  subscription: Subscription | null;
  isLoading: boolean;
  isAuthenticated: boolean;
}

interface AuthContextType extends AuthState {
  login: (email: string, password: string, tenantSlug?: string) => Promise<void>;
  logout: () => void;
  refreshMe: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    tenant: null,
    role: null,
    subscription: null,
    isLoading: true,
    isAuthenticated: false,
  });

  const refreshMe = useCallback(async () => {
    try {
      const token = getAccessToken();
      if (!token) {
        setState((prev) => ({ ...prev, isLoading: false, isAuthenticated: false }));
        return;
      }

      const { data } = await authApi.me();
      setState({
        user: data.data.user,
        tenant: data.data.tenant,
        role: data.data.role,
        subscription: data.data.subscription,
        isLoading: false,
        isAuthenticated: true,
      });
    } catch {
      setState({
        user: null,
        tenant: null,
        role: null,
        subscription: null,
        isLoading: false,
        isAuthenticated: false,
      });
    }
  }, []);

  const login = useCallback(
    async (email: string, password: string, tenantSlug?: string) => {
      const { data } = await authApi.login(email, password, tenantSlug);
      setAccessToken(data.access);
      setRefreshToken(data.refresh);
      await refreshMe();
    },
    [refreshMe]
  );

  const logout = useCallback(() => {
    clearTokens();
    setState({
      user: null,
      tenant: null,
      role: null,
      subscription: null,
      isLoading: false,
      isAuthenticated: false,
    });
  }, []);

  // При монтировании — проверяем сохранённый токен
  useEffect(() => {
    refreshMe();
  }, [refreshMe]);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, refreshMe }}>
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
