'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth-context';
import { AuthRecovery } from '@/components/auth-recovery';

export default function OnboardingPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading, hasAuthError } = useAuth();

  useEffect(() => {
    if (isLoading || hasAuthError) return;
    router.replace(isAuthenticated ? '/dashboard/settings' : '/login');
  }, [hasAuthError, isAuthenticated, isLoading, router]);

  if (hasAuthError) return <AuthRecovery />;

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
  );
}
