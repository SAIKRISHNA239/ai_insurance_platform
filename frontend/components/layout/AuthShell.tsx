'use client';

/**
 * components/layout/AuthShell.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * Renders the global Sidebar + TopHeader shell when authenticated.
 * Redirects to /login when not authenticated (except for /login itself).
 */

import { useAuth } from '@/lib/AuthContext';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect } from 'react';
import Sidebar from '@/components/layout/Sidebar';
import TopHeader from '@/components/layout/TopHeader';

const PUBLIC_ROUTES = ['/login'];

export default function AuthShell({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const pathname = usePathname();
  const router   = useRouter();

  const isPublic = PUBLIC_ROUTES.some((r) => pathname.startsWith(r));

  useEffect(() => {
    if (!isLoading && !isAuthenticated && !isPublic) {
      router.replace('/login');
    }
  }, [isLoading, isAuthenticated, isPublic, router]);

  // Loading spinner while checking localStorage
  if (isLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-4">
          <div className="w-10 h-10 rounded-full border-2 border-primary border-t-transparent animate-spin" />
          <p className="font-body-sm text-body-sm text-on-surface-variant">Checking session…</p>
        </div>
      </div>
    );
  }

  // Login page: full-screen, no shell
  if (isPublic) {
    return <>{children}</>;
  }

  // Not authenticated: show nothing while redirect fires
  if (!isAuthenticated) {
    return null;
  }

  // Authenticated: render persistent shell + page content
  return (
    <div className="flex h-screen overflow-hidden bg-background text-on-background">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <TopHeader />
        <main className="flex-1 overflow-hidden">
          {children}
        </main>
      </div>
    </div>
  );
}
