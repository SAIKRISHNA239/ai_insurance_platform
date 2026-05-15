'use client';

import { useEffect, useState } from 'react';
import { applicationsAPI, type Application } from '@/lib/api';

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; dotClass: string }> = {
  draft:        { label: 'Draft',       color: 'text-on-surface-variant', bg: 'bg-surface-variant border-outline-variant',                  dotClass: 'bg-outline' },
  submitted:    { label: 'Submitted',   color: 'text-primary',            bg: 'bg-primary-container/20 border-primary-container/30',        dotClass: 'bg-primary animate-pulse' },
  under_review: { label: 'In Review',   color: 'text-tertiary',           bg: 'bg-tertiary-container/20 border-tertiary-container/30',      dotClass: 'bg-tertiary animate-pulse' },
  approved:     { label: 'Approved',    color: 'text-secondary',          bg: 'bg-secondary-container/20 border-secondary-container/30',    dotClass: 'bg-secondary' },
  declined:     { label: 'Declined',    color: 'text-error',              bg: 'bg-error-container/20 border-error-container/30',            dotClass: 'bg-error' },
  withdrawn:    { label: 'Withdrawn',   color: 'text-on-surface-variant', bg: 'bg-surface-variant border-outline-variant',                  dotClass: 'bg-outline' },
};

const RISK_CONFIG: Record<string, string> = {
  preferred:   'bg-secondary-container/20 text-secondary border-secondary/30',
  standard:    'bg-primary-container/20 text-primary border-primary/30',
  substandard: 'bg-tertiary-container/20 text-tertiary border-tertiary/30',
  decline:     'bg-error-container/20 text-error border-error/30',
};

// ── Application Card ──────────────────────────────────────────────────────────

function AppCard({ app }: { app: Application }) {
  const cfg     = STATUS_CONFIG[app.status]  ?? STATUS_CONFIG.submitted;
  const riskCls = app.risk_tier ? (RISK_CONFIG[app.risk_tier] ?? 'bg-surface-variant') : null;

  // Map status to stepper progress
  const steps = ['Submit', 'Review', 'UW Decision', 'Closed'];
  const stepIdx = { draft: 0, submitted: 1, under_review: 2, approved: 3, declined: 3, withdrawn: 3 }[app.status] ?? 1;

  return (
    <div className="glass-card rounded-2xl p-6 flex flex-col gap-6 shadow-lg hover:shadow-xl transition-all duration-300 hover:-translate-y-1">
      {/* Card header */}
      <div className="flex justify-between items-start">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center text-on-surface shadow-inner">
            <span className="material-symbols-outlined">person</span>
          </div>
          <div>
            <h3 className="font-headline-md text-[20px] text-on-surface">{app.application_number}</h3>
            <p className="font-data-mono text-on-surface-variant mt-1 capitalize">
              {app.policy_type} · ${parseFloat(app.requested_coverage_limit).toLocaleString()} coverage
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {riskCls && (
            <span className={`px-2.5 py-1 rounded-full border font-label-caps text-label-caps ${riskCls}`}>
              {app.risk_tier?.toUpperCase()}
            </span>
          )}
          <span className={`px-2.5 py-1 rounded-full border font-label-caps text-label-caps flex items-center gap-1.5 ${cfg.bg} ${cfg.color}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${cfg.dotClass}`} />
            {cfg.label}
          </span>
        </div>
      </div>

      {/* Stepper */}
      <div className="w-full flex items-start gap-0 relative mt-2">
        <div className="absolute left-3 top-3 right-3 h-[2px] bg-white/10 -z-10" />
        <div
          className="absolute left-3 top-3 h-[2px] bg-secondary shadow-[0_0_8px_currentColor] -z-10 transition-all duration-500"
          style={{ width: `${(stepIdx / (steps.length - 1)) * (100 - 0)}%` }}
        />
        {steps.map((label, i) => (
          <div key={label} className="flex flex-col items-center flex-1">
            {i < stepIdx ? (
              <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center z-10 border-[3px] border-[#10141a] shadow-[0_0_12px_rgba(40,167,69,0.4)]">
                <span className="material-symbols-outlined text-on-secondary text-[14px]">check</span>
              </div>
            ) : i === stepIdx ? (
              <div className="w-6 h-6 rounded-full bg-[#10141a] border-2 border-secondary z-10 flex items-center justify-center shadow-[0_0_12px_rgba(40,167,69,0.4)]">
                <div className="w-2 h-2 rounded-full bg-secondary" />
              </div>
            ) : (
              <div className="w-6 h-6 rounded-full bg-white/10 z-10 border-[3px] border-[#10141a]" />
            )}
            <span className={`font-label-caps mt-2 whitespace-nowrap tracking-wider text-[10px] ${i <= stepIdx ? 'text-on-surface' : 'text-on-surface-variant'}`}>
              {label}
            </span>
          </div>
        ))}
      </div>

      {/* AI notes */}
      {app.ai_underwriting_notes && (
        <div className="border-t border-white/10 pt-5 mt-2">
          <p className="font-label-caps text-on-surface-variant uppercase tracking-widest mb-2 flex items-center gap-2">
            <span className="material-symbols-outlined text-[16px] text-tertiary">auto_awesome</span>
            AI Assessment
          </p>
          <p className="font-body-sm text-on-surface leading-relaxed line-clamp-2">
            {app.ai_underwriting_notes}
          </p>
        </div>
      )}

      {/* Score bar */}
      {app.underwriting_score !== null && (
        <div className="flex items-center gap-4 bg-black/20 p-4 rounded-xl border border-white/5">
          <div className="flex-1 h-2 bg-white/5 rounded-full overflow-hidden shadow-inner">
            <div
              className="h-full rounded-full bg-gradient-to-r from-secondary to-error transition-all"
              style={{ width: `${Math.min(100, app.underwriting_score)}%` }}
            />
          </div>
          <span className="font-data-mono text-on-surface tabular-nums shrink-0 drop-shadow-md">
            Score: {Math.round(app.underwriting_score)}
          </span>
          <a
            href="/underwriting"
            className="shrink-0 px-4 py-2 bg-primary/20 border border-primary/30 text-primary font-label-caps rounded-full hover:bg-primary hover:text-on-primary transition-all flex items-center gap-1"
          >
            Review <span className="material-symbols-outlined text-[14px]">arrow_forward</span>
          </a>
        </div>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function ApplicationsPage() {
  const [apps,    setApps]    = useState<Application[]>([]);
  const [total,   setTotal]   = useState(0);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);

  useEffect(() => {
    applicationsAPI.list(1, 20)
      .then((res) => { setApps(res.items); setTotal(res.total); })
      .catch((e) => setError(String(e?.detail ?? e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="h-full overflow-hidden relative p-8">
      {/* Mesh floating backgrounds */}
      <div className="absolute top-[20%] right-[10%] w-[450px] h-[450px] bg-tertiary/10 rounded-full blur-[100px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[10%] left-[5%] w-[400px] h-[400px] bg-secondary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '4s' }} />

      <div className="max-w-[1440px] mx-auto relative z-10 h-full overflow-y-auto no-scrollbar">

        {/* ── Header ──────────────────────────────────────────────────── */}
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 mb-8">
          <div>
            <h2 className="font-display text-[40px] text-on-surface leading-tight tracking-tight drop-shadow-md">Active Application Pipeline</h2>
            <p className="font-body-lg text-on-surface-variant mt-2">
              {loading ? '…' : `${total} applications`} · Manage and track underwriting requests.
            </p>
          </div>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-4 rounded-lg bg-error-container/20 border border-error-container/30 px-4 py-3">
            <p className="font-body-sm text-body-sm text-error">⚠ {error}</p>
          </div>
        )}

        {/* ── Cards ───────────────────────────────────────────────────── */}
        <div className="flex flex-col gap-4">
          {loading
            ? Array.from({ length: 3 }).map((_, i) => (
                <div key={i} className="h-40 rounded-xl bg-surface-container animate-pulse" />
              ))
            : apps.length === 0
            ? (
              <div className="flex flex-col items-center gap-4 py-16 text-center">
                <span className="material-symbols-outlined text-on-surface-variant text-4xl">description</span>
                <p className="font-body-sm text-body-sm text-on-surface-variant">No applications found.</p>
              </div>
            )
            : apps.map((app) => <AppCard key={app.id} app={app} />)
          }
        </div>
      </div>
    </div>
  );
}
