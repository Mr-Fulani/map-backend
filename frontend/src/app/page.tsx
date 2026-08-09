/**
 * Корневая страница: redirect на /dashboard или /login.
 */

'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth-context';
import { AuthRecovery } from '@/components/auth-recovery';

export default function HomePage() {
  const { isAuthenticated, isLoading, hasAuthError } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && !hasAuthError) {
      router.replace(isAuthenticated ? '/dashboard' : '/login');
    }
  }, [hasAuthError, isAuthenticated, isLoading, router]);

  if (hasAuthError) return <AuthRecovery />;

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
  );
}
