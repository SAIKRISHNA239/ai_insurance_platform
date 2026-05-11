'use client';

import { usePathname } from 'next/navigation';
import { useAuth } from '@/lib/AuthContext';

const PAGE_LABELS: Record<string, string> = {
  '/overview':     'Overview',
  '/claims':       'Claims Center',
  '/marketplace':  'Marketplace',
  '/applications': 'Applications',
  '/underwriting': 'Underwriting Desk',
  '/settings':     'Settings',
  '/support':      'Support',
};

function getPageLabel(pathname: string): string {
  // Exact root match first, then prefix match
  if (pathname === '/') return PAGE_LABELS['/'];
  const match = Object.entries(PAGE_LABELS).find(
    ([path]) => path !== '/' && pathname.startsWith(path)
  );
  return match?.[1] ?? 'MedIntelligence';
}

export default function TopHeader() {
  const { logout, role }  = useAuth();
  const pathname          = usePathname();
  const label             = getPageLabel(pathname);

  return (
    <header
      className="
        sticky top-0 z-50 shrink-0
        flex items-center justify-between
        px-8 h-16 w-full
        border-b border-outline-variant
        bg-surface/80 backdrop-blur-md
      "
    >
      {/* ── Search ──────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-4 flex-1">
        <div className="relative flex items-center">
          <span className="material-symbols-outlined absolute left-3 text-on-surface-variant pointer-events-none">
            search
          </span>
          <input
            type="text"
            placeholder={`Search ${label.toLowerCase()}...`}
            className="
              w-72 bg-surface-container-low border border-outline-variant rounded-lg
              pl-10 pr-4 py-1.5
              font-body-sm text-body-sm text-on-surface placeholder:text-on-surface-variant
              focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary
              transition-all
            "
          />
        </div>
      </div>

      {/* ── Actions + Profile ───────────────────────────────────────────── */}
      <div className="flex items-center gap-6">
        {/* Icon buttons */}
        <div className="flex items-center gap-1 text-on-surface-variant">
          <button
            className="w-10 h-10 rounded-full hover:bg-surface-variant flex items-center justify-center hover:text-primary transition-colors"
            aria-label="Notifications"
          >
            <span className="material-symbols-outlined">notifications_none</span>
          </button>
          <button
            className="w-10 h-10 rounded-full hover:bg-surface-variant flex items-center justify-center hover:text-primary transition-colors"
            aria-label="Admin settings"
          >
            <span className="material-symbols-outlined">admin_panel_settings</span>
          </button>
        </div>

        <div className="h-6 w-px bg-outline-variant" />

        {/* User profile + logout */}
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-3 p-1.5 rounded-lg">
            <div className="text-right hidden lg:block">
              <span className="block font-body-sm text-body-sm font-semibold text-on-surface capitalize">
                {role ?? 'User'}
              </span>
              <span className="block font-label-caps text-label-caps text-on-surface-variant">
                Active Session
              </span>
            </div>
            <div className="w-9 h-9 rounded-full bg-primary-container flex items-center justify-center shrink-0 border border-outline">
              <span className="material-symbols-outlined text-on-primary-container">person</span>
            </div>
          </div>
          <button
            onClick={() => { logout(); window.location.href = '/login'; }}
            className="w-9 h-9 rounded-full hover:bg-error-container/20 flex items-center justify-center text-on-surface-variant hover:text-error transition-colors"
            aria-label="Log out"
            title="Log out"
          >
            <span className="material-symbols-outlined text-[20px]">logout</span>
          </button>
        </div>
      </div>
    </header>
  );
}
