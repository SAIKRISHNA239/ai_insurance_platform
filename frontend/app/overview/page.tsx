'use client';

import { useEffect, useState } from 'react';
import {
  getDashboardStats, claimsAPI, applicationsAPI,
  type Claim, type Application, type DashboardStats,
} from '@/lib/api';

// ── Status display helpers ────────────────────────────────────────────────────

const CLAIM_STATUS_CONFIG: Record<string, { label: string; dot: string; badge: string }> = {
  submitted:          { label: 'Submitted',        dot: 'bg-outline',                badge: 'bg-surface-variant text-on-surface-variant border-outline-variant' },
  in_review:          { label: 'In Review',         dot: 'bg-tertiary animate-pulse', badge: 'bg-tertiary-container/20 text-tertiary border-tertiary-container/30' },
  pending_info:       { label: 'Pending Info',      dot: 'bg-tertiary',              badge: 'bg-tertiary-container/20 text-tertiary border-tertiary-container/30' },
  approved:           { label: 'Approved',          dot: 'bg-secondary',             badge: 'bg-secondary-container/20 text-secondary border-secondary-container/30' },
  partially_approved: { label: 'Part. Approved',    dot: 'bg-secondary',             badge: 'bg-secondary-container/20 text-secondary border-secondary-container/30' },
  denied:             { label: 'Denied',            dot: 'bg-error',                 badge: 'bg-error-container/20 text-error border-error-container/30' },
  appealed:           { label: 'Appealed',          dot: 'bg-primary',               badge: 'bg-primary-container/20 text-primary border-primary-container/30' },
  closed:             { label: 'Closed',            dot: 'bg-outline',               badge: 'bg-surface-variant text-on-surface-variant border-outline-variant' },
};

const APP_STATUS_CONFIG: Record<string, { label: string; dot: string; badge: string }> = {
  draft:        { label: 'Draft',       dot: 'bg-outline',   badge: 'bg-surface-variant text-on-surface-variant border-outline-variant' },
  submitted:    { label: 'Submitted',   dot: 'bg-primary',   badge: 'bg-primary-container/20 text-primary border-primary-container/30' },
  under_review: { label: 'In Review',   dot: 'bg-tertiary animate-pulse', badge: 'bg-tertiary-container/20 text-tertiary border-tertiary-container/30' },
  approved:     { label: 'Approved',    dot: 'bg-secondary', badge: 'bg-secondary-container/20 text-secondary border-secondary-container/30' },
  declined:     { label: 'Declined',    dot: 'bg-error',     badge: 'bg-error-container/20 text-error border-error-container/30' },
  withdrawn:    { label: 'Withdrawn',   dot: 'bg-outline',   badge: 'bg-surface-variant text-on-surface-variant border-outline-variant' },
};

function StatusBadge({ status, type }: { status: string; type: 'claim' | 'app' }) {
  const cfg = type === 'claim'
    ? (CLAIM_STATUS_CONFIG[status] ?? CLAIM_STATUS_CONFIG.submitted)
    : (APP_STATUS_CONFIG[status]  ?? APP_STATUS_CONFIG.submitted);
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border font-label-caps text-label-caps ${cfg.badge}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
      {cfg.label}
    </span>
  );
}

function SkeletonRow({ cols }: { cols: number }) {
  return (
    <tr>
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-5 py-3">
          <div className="h-3 rounded bg-surface-container animate-pulse w-3/4" />
        </td>
      ))}
    </tr>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function OverviewPage() {
  const [stats,   setStats]   = useState<DashboardStats | null>(null);
  const [claims,  setClaims]  = useState<Claim[]>([]);
  const [apps,    setApps]    = useState<Application[]>([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getDashboardStats(),
      claimsAPI.list(1).then((r) => r.items.slice(0, 4)),
      applicationsAPI.list(1, 4).then((r) => r.items),
    ])
      .then(([s, c, a]) => { setStats(s); setClaims(c); setApps(a); })
      .catch((e) => setError(String(e?.detail ?? e)))
      .finally(() => setLoading(false));
  }, []);

  const kpiCards = stats ? [
    {
      label: 'Total Claims',
      value: stats.totalClaims.toLocaleString(),
      icon: 'assignment_turned_in',
      color: 'text-primary',
      trend: `${stats.approvedClaims} approved`,
      trendColor: 'text-secondary',
      trendIcon: 'check_circle',
    },
    {
      label: 'Active Applications',
      value: stats.totalApplications.toLocaleString(),
      icon: 'folder_open',
      color: 'text-tertiary',
      trend: `${stats.pendingReview} pending review`,
      trendColor: 'text-tertiary',
      trendIcon: 'pending',
    },
    {
      label: 'Approval Rate',
      value: stats.totalClaims > 0
        ? `${Math.round((stats.approvedClaims / stats.totalClaims) * 100)}%`
        : '—',
      icon: 'smart_toy',
      color: 'text-secondary',
      trend: 'AI adjudication',
      trendColor: 'text-secondary',
      trendIcon: 'auto_awesome',
    },
  ] : [];

  return (
    <div className="h-full overflow-hidden relative p-8">
      {/* Mesh floating backgrounds */}
      <div className="absolute top-[5%] right-[15%] w-[400px] h-[400px] bg-secondary/10 rounded-full blur-[100px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[20%] left-[10%] w-[500px] h-[500px] bg-primary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '3s' }} />

      <div className="max-w-[1440px] mx-auto flex flex-col gap-8 relative z-10 h-full overflow-y-auto no-scrollbar">

        {/* ── Page Header ─────────────────────────────────────────────── */}
        <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
          <div>
            <h2 className="font-display text-[40px] text-on-surface leading-tight tracking-tight drop-shadow-md">System Overview</h2>
            <p className="font-body-lg text-on-surface-variant mt-2">
              Real-time intelligence and claims activity.
            </p>
          </div>
        </div>

        {/* ── Error ───────────────────────────────────────────────────── */}
        {error && (
          <div className="rounded-lg bg-error-container/20 border border-error-container/30 px-4 py-3">
            <p className="font-body-sm text-body-sm text-error">⚠ {error}</p>
          </div>
        )}

        {/* ── KPI Cards ───────────────────────────────────────────────── */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {loading
            ? Array.from({ length: 3 }).map((_, i) => (
                <div key={i} className="h-32 rounded-xl bg-surface-container animate-pulse" />
              ))
            : kpiCards.map((card) => (
                <div
                  key={card.label}
                  className="glass-card rounded-2xl p-6 flex flex-col gap-4 relative overflow-hidden group transition-all duration-300 hover:-translate-y-1"
                >
                  <div className="absolute inset-0 bg-gradient-to-br from-primary/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
                  <div className="flex justify-between items-start">
                    <h3 className="font-body-sm text-on-surface-variant uppercase tracking-wider font-semibold">{card.label}</h3>
                    <div className="w-10 h-10 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center">
                      <span className={`material-symbols-outlined text-[20px] ${card.color}`}>{card.icon}</span>
                    </div>
                  </div>
                  <span className="font-display text-[48px] text-on-surface tracking-tight drop-shadow-sm">{card.value}</span>
                  <div className={`flex items-center text-sm ${card.trendColor}`}>
                    <span className="material-symbols-outlined text-sm mr-1">{card.trendIcon}</span>
                    <span className="font-label-caps">{card.trend}</span>
                  </div>
                </div>
              ))
          }
        </div>

        {/* ── Main Grid ───────────────────────────────────────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

          {/* Recent Claims */}
          <div className="glass-panel rounded-2xl flex flex-col overflow-hidden transition-all duration-300 hover:shadow-2xl">
            <div className="px-6 py-5 border-b border-white/5 flex justify-between items-center bg-white/[0.02]">
              <h3 className="font-headline-md text-[18px] text-on-surface">Recent Claims</h3>
              <a href="/claims" className="font-label-caps px-4 py-2 rounded-full border border-white/10 bg-white/5 text-primary flex items-center gap-1 hover:bg-white/10 hover:text-primary-container transition-all">
                View All <span className="material-symbols-outlined text-[14px]">chevron_right</span>
              </a>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-black/20 border-b border-white/5 font-label-caps text-on-surface-variant tracking-wider uppercase">
                    {['Claim #', 'Billed', 'Status'].map((h) => (
                      <th key={h} className="px-6 py-4 font-semibold">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="font-body-sm divide-y divide-white/5">
                  {loading
                    ? Array.from({ length: 4 }).map((_, i) => <SkeletonRow key={i} cols={3} />)
                    : claims.length === 0
                    ? (
                      <tr>
                        <td colSpan={3} className="px-5 py-6 text-center text-on-surface-variant font-body-sm text-body-sm">
                          No claims found
                        </td>
                      </tr>
                    )
                    : claims.map((c) => (
                      <tr key={c.id} className="hover:bg-surface-variant/50 transition-colors">
                        <td className="px-5 py-3 font-data-mono text-data-mono text-on-surface">{c.claim_number}</td>
                        <td className="px-5 py-3 font-data-mono text-data-mono text-on-surface-variant">${parseFloat(c.billed_amount).toLocaleString()}</td>
                        <td className="px-5 py-3"><StatusBadge status={c.status} type="claim" /></td>
                      </tr>
                    ))
                  }
                </tbody>
              </table>
            </div>
          </div>

          {/* Recent Applications */}
          <div className="glass-panel rounded-2xl flex flex-col overflow-hidden transition-all duration-300 hover:shadow-2xl">
            <div className="px-6 py-5 border-b border-white/5 flex justify-between items-center bg-white/[0.02]">
              <h3 className="font-headline-md text-[18px] text-on-surface">Recent Applications</h3>
              <a href="/applications" className="font-label-caps px-4 py-2 rounded-full border border-white/10 bg-white/5 text-primary flex items-center gap-1 hover:bg-white/10 hover:text-primary-container transition-all">
                View All <span className="material-symbols-outlined text-[14px]">chevron_right</span>
              </a>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-black/20 border-b border-white/5 font-label-caps text-on-surface-variant tracking-wider uppercase">
                    {['App #', 'Score', 'Status'].map((h) => (
                      <th key={h} className="px-6 py-4 font-semibold">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="font-body-sm divide-y divide-white/5">
                  {loading
                    ? Array.from({ length: 4 }).map((_, i) => <SkeletonRow key={i} cols={3} />)
                    : apps.length === 0
                    ? (
                      <tr>
                        <td colSpan={3} className="px-5 py-6 text-center text-on-surface-variant font-body-sm text-body-sm">
                          No applications found
                        </td>
                      </tr>
                    )
                    : apps.map((a) => (
                      <tr key={a.id} className="hover:bg-surface-variant/50 transition-colors">
                        <td className="px-5 py-3 font-data-mono text-data-mono text-on-surface">{a.application_number}</td>
                        <td className="px-5 py-3">
                          {a.underwriting_score !== null ? (
                            <div className="flex items-center gap-2">
                              <div className="w-16 h-1.5 bg-surface-container-highest rounded-full overflow-hidden">
                                <div
                                  className="h-full rounded-full bg-gradient-to-r from-secondary to-error"
                                  style={{ width: `${Math.min(100, a.underwriting_score)}%` }}
                                />
                              </div>
                              <span className="font-data-mono text-data-mono text-on-surface-variant">{Math.round(a.underwriting_score)}</span>
                            </div>
                          ) : (
                            <span className="text-on-surface-variant">—</span>
                          )}
                        </td>
                        <td className="px-5 py-3"><StatusBadge status={a.status} type="app" /></td>
                      </tr>
                    ))
                  }
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
