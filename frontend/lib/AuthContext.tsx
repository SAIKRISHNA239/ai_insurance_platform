'use client';

/**
 * lib/AuthContext.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * Global auth state. Wraps the entire app via layout.tsx.
 *
 * FIX: The original version called getStoredToken() during component init,
 * which ran on the server (SSR) and crashed because localStorage is browser-only.
 * Now uses a useEffect to hydrate auth state client-side only.
 */

import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { authAPI, setStoredToken, clearStoredToken } from '@/lib/api';

interface AuthContextValue {
  isAuthenticated: boolean;
  isLoading: boolean;
  role: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue>({
  isAuthenticated: false,
  isLoading: true,
  role: null,
  login: async () => {},
  logout: () => {},
});

function decodeTokenPayload(token: string): { role?: string; exp?: number } | null {
  try {
    return JSON.parse(atob(token.split('.')[1]));
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Start as loading=true, isAuthenticated=false — hydrated in useEffect
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading]             = useState(true);
  const [role, setRole]                       = useState<string | null>(null);

  // ── Restore session client-side only ──────────────────────────────────────
  useEffect(() => {
    const token = localStorage.getItem('access_token');
    if (token) {
      const payload = decodeTokenPayload(token);
      const isExpired = payload?.exp && payload.exp * 1000 < Date.now();
      if (payload && !isExpired) {
        setIsAuthenticated(true);
        setRole(payload.role ?? null);
      } else {
        localStorage.removeItem('access_token');
      }
    }
    setIsLoading(false);
  }, []);

  // ── login ──────────────────────────────────────────────────────────────────
  const login = useCallback(async (email: string, password: string) => {
    const data = await authAPI.login(email, password);
    setStoredToken(data.access_token);
    const payload = decodeTokenPayload(data.access_token);
    setIsAuthenticated(true);
    setRole(payload?.role ?? data.role ?? null);
  }, []);

  // ── logout ─────────────────────────────────────────────────────────────────
  const logout = useCallback(() => {
    clearStoredToken();
    setIsAuthenticated(false);
    setRole(null);
  }, []);

  return (
    <AuthContext.Provider value={{ isAuthenticated, isLoading, role, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
