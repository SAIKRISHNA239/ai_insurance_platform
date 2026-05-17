'use client';

import { useEffect, useState } from 'react';
import { policiesAPI, type Policy, type PolicyStatus } from '@/lib/api';

// ── Status config ─────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<PolicyStatus, { label: string; dot: string; badge: string }> = {
  pending:   { label: 'Pending',   dot: 'bg-primary animate-pulse',  badge: 'bg-primary-container/20 text-primary border-primary-container/30' },
  active:    { label: 'Active',    dot: 'bg-secondary',              badge: 'bg-secondary-container/20 text-secondary border-secondary-container/30' },
  lapsed:    { label: 'Lapsed',    dot: 'bg-tertiary',               badge: 'bg-tertiary-container/20 text-tertiary border-tertiary-container/30' },
  cancelled: { label: 'Cancelled', dot: 'bg-error',                  badge: 'bg-error-container/20 text-error border-error-container/30' },
  expired:   { label: 'Expired',   dot: 'bg-outline',                badge: 'bg-surface-variant text-on-surface-variant border-outline-variant' },
};

const TYPE_LABELS: Record<string, string> = {
  individual:          'Individual',
  group:               'Group',
  medicare_supplement: 'Medicare Supp.',
  dental:              'Dental',
  vision:              'Vision',
};

// ── Page ──────────────────────────────────────────────────────────────────────

export default function PoliciesPage() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [total,    setTotal]    = useState(0);
  const [loading,  setLoading]  = useState(true);
  const [error,    setError]    = useState<string | null>(null);
  const [filter,   setFilter]   = useState<PolicyStatus | ''>('');
  const [search,   setSearch]   = useState('');

  function loadPolicies(status?: PolicyStatus) {
    setLoading(true);
    policiesAPI.list(1, status)
      .then((res) => { setPolicies(res.items); setTotal(res.total); })
      .catch((e) => setError(String(e?.detail ?? e)))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadPolicies(filter as PolicyStatus || undefined); }, [filter]);

  const displayed = search
    ? policies.filter((p) =>
        p.policy_number.toLowerCase().includes(search.toLowerCase()) ||
        TYPE_LABELS[p.policy_type]?.toLowerCase().includes(search.toLowerCase())
      )
    : policies;

  return (
    <div className="h-full overflow-hidden relative p-8">
      {/* Mesh floating backgrounds */}
      <div className="absolute top-[15%] left-[5%] w-[450px] h-[450px] bg-secondary/10 rounded-full blur-[110px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[10%] right-[8%] w-[400px] h-[400px] bg-primary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '2.5s' }} />

      <div className="max-w-[1440px] mx-auto space-y-8 relative z-10 h-full overflow-y-auto no-scrollbar">

        {/* ── Page Header ──────────────────────────────────────────────── */}
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <h1 className="font-display text-[40px] text-on-surface leading-tight tracking-tight drop-shadow-md">
              Policy Registry
            </h1>
            <p className="font-body-lg text-on-surface-variant mt-2">
              {loading ? '…' : `${total} policies`} · Review active and historical insurance contracts.
            </p>
          </div>

          {/* Status filter + Search */}
          <div className="flex items-center gap-3 flex-wrap">
            <select
              id="policy-status-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value as PolicyStatus | '')}
              className="px-4 py-2.5 bg-surface-container border border-white/10 rounded-xl text-on-surface font-label-caps text-sm outline-none focus:border-primary/50 transition-all cursor-pointer"
            >
              <option value="">All Statuses</option>
              {Object.entries(STATUS_CONFIG).map(([v, { label }]) => (
                <option key={v} value={v}>{label}</option>
              ))}
            </select>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 material-symbols-outlined text-on-surface-variant text-[18px]">search</span>
              <input
                id="policy-search"
                type="text"
                placeholder="Search policy #, type…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-9 pr-4 py-2.5 bg-surface-container border border-white/10 rounded-xl text-on-surface font-body-sm text-sm placeholder:text-on-surface-variant/40 outline-none focus:border-primary/50 transition-all w-56"
              />
            </div>
          </div>
        </div>

        {/* ── Error ────────────────────────────────────────────────────── */}
        {error && (
          <div className="rounded-xl bg-error-container/20 border border-error-container/30 px-4 py-3">
            <p className="font-body-sm text-error">⚠ {error}</p>
          </div>
        )}

        {/* ── Table ────────────────────────────────────────────────────── */}
        <div className="glass-card rounded-2xl overflow-hidden shadow-xl">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-white/10">
                  {['Policy #', 'Type', 'Premium / mo', 'Coverage Limit', 'Effective', 'Expires', 'Status'].map((h) => (
                    <th
                      key={h}
                      className="px-6 py-4 text-left font-label-caps text-on-surface-variant text-[11px] uppercase tracking-widest whitespace-nowrap"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <tr key={i} className="border-b border-white/5">
                      {Array.from({ length: 7 }).map((_, j) => (
                        <td key={j} className="px-6 py-4">
                          <div className="h-4 rounded bg-white/5 animate-pulse" style={{ width: `${40 + (j * 11) % 50}%` }} />
                        </td>
                      ))}
                    </tr>
                  ))
                ) : displayed.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-6 py-20 text-center">
                      <span className="material-symbols-outlined text-on-surface-variant text-4xl block mb-3">folder_open</span>
                      <p className="font-body-sm text-on-surface-variant">No policies found.</p>
                    </td>
                  </tr>
                ) : (
                  displayed.map((policy, idx) => {
                    const cfg = STATUS_CONFIG[policy.status] ?? STATUS_CONFIG.pending;
                    return (
                      <tr
                        key={policy.id}
                        className={`border-b border-white/5 hover:bg-white/3 transition-colors ${idx % 2 === 0 ? '' : 'bg-white/[0.01]'}`}
                      >
                        {/* Policy # */}
                        <td className="px-6 py-4">
                          <span className="font-data-mono text-on-surface text-sm">{policy.policy_number}</span>
                        </td>

                        {/* Type */}
                        <td className="px-6 py-4">
                          <span className="font-body-sm text-on-surface-variant capitalize text-sm">
                            {TYPE_LABELS[policy.policy_type] ?? policy.policy_type}
                          </span>
                        </td>

                        {/* Premium */}
                        <td className="px-6 py-4">
                          <span className="font-data-mono text-on-surface text-sm">
                            ${parseFloat(policy.premium_amount).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                          </span>
                        </td>

                        {/* Coverage */}
                        <td className="px-6 py-4">
                          <span className="font-data-mono text-secondary text-sm">
                            ${parseFloat(policy.coverage_limit).toLocaleString('en-US', { maximumFractionDigits: 0 })}
                          </span>
                        </td>

                        {/* Effective */}
                        <td className="px-6 py-4">
                          <span className="font-data-mono text-on-surface-variant text-sm">
                            {new Date(policy.effective_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                          </span>
                        </td>

                        {/* Expiry */}
                        <td className="px-6 py-4">
                          <span className="font-data-mono text-on-surface-variant text-sm">
                            {new Date(policy.expiry_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                          </span>
                        </td>

                        {/* Status badge */}
                        <td className="px-6 py-4">
                          <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border font-label-caps text-[11px] ${cfg.badge}`}>
                            <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
                            {cfg.label}
                          </span>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {/* Table footer */}
          {!loading && displayed.length > 0 && (
            <div className="px-6 py-4 border-t border-white/10 flex justify-between items-center">
              <p className="font-label-caps text-on-surface-variant text-[11px]">
                Showing {displayed.length} of {total} policies
              </p>
              <p className="font-label-caps text-on-surface-variant/40 text-[11px]">
                Sorted by creation date, newest first
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
