'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

// ── Navigation items matching the prototype sidebar ───────────────────────────
const NAV_ITEMS = [
  { label: 'Overview',           icon: 'dashboard',   href: '/overview'     },
  { label: 'Claims Center',      icon: 'assignment',  href: '/claims'       },
  { label: 'Marketplace',        icon: 'storefront',  href: '/marketplace'  },
  { label: 'Applications',       icon: 'description', href: '/applications' },
  { label: 'Underwriting Desk',  icon: 'security',    href: '/underwriting' },
] as const;

const FOOTER_ITEMS = [
  { label: 'Settings', icon: 'settings',     href: '/settings' },
  { label: 'Support',  icon: 'help_outline', href: '/support'  },
] as const;

function isActive(href: string, pathname: string): boolean {
  return href === '/' ? pathname === '/' : pathname.startsWith(href);
}

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <nav
      className="
        hidden md:flex flex-col h-screen w-64 shrink-0
        border-r border-white/5 glass-panel py-8 z-40 shadow-[4px_0_24px_rgba(0,0,0,0.2)]
      "
    >
      {/* ── Logo ────────────────────────────────────────────────────────── */}
      <div className="px-6 mb-8 flex items-center gap-3">
        <div className="w-8 h-8 rounded bg-primary flex items-center justify-center shrink-0">
          <span
            className="material-symbols-outlined text-on-primary text-xl"
            style={{ fontVariationSettings: "'FILL' 1" }}
          >
            security
          </span>
        </div>
        <div>
          <h1 className="font-headline-md text-headline-md font-bold text-primary leading-tight">
            MedIntelligence
          </h1>
          <p className="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider">
            Enterprise Analytics
          </p>
        </div>
      </div>

      {/* ── Main navigation ─────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col gap-1 px-4 overflow-y-auto">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href, pathname);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`
                relative flex items-center gap-3 px-4 py-3 rounded-xl
                font-body-sm tracking-tight transition-all duration-300
                ${active
                  ? 'text-secondary font-bold bg-white/10 shadow-sm backdrop-blur-sm'
                  : 'text-on-surface-variant hover:bg-white/5 hover:text-on-surface hover:pl-5'}
              `}
            >
              {/* Active indicator bar */}
              {active && (
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-secondary rounded-r-full" />
              )}
              <span
                className="material-symbols-outlined text-xl"
                style={active ? { fontVariationSettings: "'FILL' 1" } : undefined}
              >
                {item.icon}
              </span>
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>

      {/* ── Footer ──────────────────────────────────────────────────────── */}
      <div className="mt-auto px-4 pt-4 border-t border-outline-variant flex flex-col gap-1">
        {FOOTER_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className="
              flex items-center gap-3 px-4 py-3 rounded-lg
              font-body-sm text-body-sm tracking-tight
              text-on-surface-variant hover:bg-surface-variant transition-colors group
            "
          >
            <span className="material-symbols-outlined text-xl group-hover:text-primary transition-colors">
              {item.icon}
            </span>
            <span>{item.label}</span>
          </Link>
        ))}
      </div>
    </nav>
  );
}
