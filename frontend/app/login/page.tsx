'use client';

/**
 * app/login/page.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * Login page. Does NOT render inside the global Sidebar/TopHeader layout
 * because it uses a separate route group (see app/(auth)/login/page.tsx note).
 * The root layout wraps this page but the sidebar/header check isAuthenticated.
 */

import { useAuth } from '@/lib/AuthContext';
import { useRouter } from 'next/navigation';
import { useState, FormEvent } from 'react';

export default function LoginPage() {
  const { login }         = useAuth();
  const router            = useRouter();
  const [email, setEmail] = useState('');
  const [pass, setPass]   = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(email, pass);
      router.replace('/overview');
    } catch (err: unknown) {
      const detail = (err as { detail?: string })?.detail;
      setError(detail ?? 'Login failed. Check your credentials.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      {/* Gradient glow */}
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 w-[600px] h-[300px] bg-primary/10 rounded-full blur-[80px] pointer-events-none" />

      <div className="relative w-full max-w-md">
        {/* Logo */}
        <div className="flex items-center gap-3 justify-center mb-10">
          <div className="w-10 h-10 rounded-xl bg-primary flex items-center justify-center">
            <span className="material-symbols-outlined text-on-primary text-2xl" style={{ fontVariationSettings: "'FILL' 1" }}>
              security
            </span>
          </div>
          <div>
            <p className="font-headline-lg text-headline-lg font-bold text-primary leading-tight">MedIntelligence</p>
            <p className="font-label-caps text-label-caps text-on-surface-variant uppercase">Enterprise Analytics</p>
          </div>
        </div>

        {/* Card */}
        <div className="bg-surface-container-low border border-outline-variant rounded-2xl p-8 shadow-2xl shadow-black/30">
          <h1 className="font-headline-md text-headline-md text-on-surface mb-1">Welcome back</h1>
          <p className="font-body-sm text-body-sm text-on-surface-variant mb-8">
            Sign in to your underwriting workstation.
          </p>

          <form onSubmit={handleSubmit} className="flex flex-col gap-5">
            {/* Email */}
            <div className="flex flex-col gap-1.5">
              <label htmlFor="email" className="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="admin@medintel.ai"
                className="
                  w-full bg-surface border border-outline-variant rounded-lg
                  px-4 py-2.5 font-body-sm text-body-sm text-on-surface
                  placeholder:text-on-surface-variant/50
                  focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary
                  transition-all
                "
              />
            </div>

            {/* Password */}
            <div className="flex flex-col gap-1.5">
              <label htmlFor="password" className="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                autoComplete="current-password"
                value={pass}
                onChange={(e) => setPass(e.target.value)}
                placeholder="••••••••"
                className="
                  w-full bg-surface border border-outline-variant rounded-lg
                  px-4 py-2.5 font-body-sm text-body-sm text-on-surface
                  placeholder:text-on-surface-variant/50
                  focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary
                  transition-all
                "
              />
            </div>

            {/* Error */}
            {error && (
              <div className="rounded-lg bg-error-container/20 border border-error-container/30 px-3 py-2">
                <p className="font-body-sm text-body-sm text-error">{error}</p>
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={loading}
              className="
                w-full bg-primary text-on-primary font-body-sm text-body-sm font-semibold
                py-3 rounded-lg hover:opacity-90 transition-opacity
                disabled:opacity-50 disabled:cursor-not-allowed
                focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-surface-container-low
              "
            >
              {loading ? 'Signing in…' : 'Sign In'}
            </button>
          </form>

          {/* Dev hint */}
          <div className="mt-6 pt-5 border-t border-outline-variant">
            <p className="font-label-caps text-label-caps text-on-surface-variant text-center mb-3">
              DEV CREDENTIALS
            </p>
            <div className="grid grid-cols-2 gap-2">
              {[
                { label: 'Admin', email: 'admin@medintel.ai', pass: 'Admin1234!' },
                { label: 'Underwriter', email: 'uw@medintel.ai', pass: 'Underwriter1!' },
              ].map((cred) => (
                <button
                  key={cred.label}
                  type="button"
                  onClick={() => { setEmail(cred.email); setPass(cred.pass); }}
                  className="
                    flex flex-col items-start px-3 py-2 rounded-lg
                    bg-surface border border-outline-variant
                    hover:border-primary/40 hover:bg-primary/5
                    transition-colors text-left
                  "
                >
                  <span className="font-body-sm text-body-sm text-on-surface font-medium">{cred.label}</span>
                  <span className="font-data-mono text-data-mono text-on-surface-variant truncate w-full">{cred.email}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
