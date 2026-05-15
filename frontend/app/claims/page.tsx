'use client';

import { useEffect, useState } from 'react';
import { claimsAPI, type Claim, type ClaimStatus } from '@/lib/api';

// ── Status config ─────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<ClaimStatus, { label: string; dot: string; badge: string }> = {
  submitted:          { label: 'Submitted',       dot: 'bg-primary animate-pulse',  badge: 'bg-primary-container/20 text-primary border-primary-container/30' },
  in_review:          { label: 'In Review',        dot: 'bg-tertiary animate-pulse', badge: 'bg-tertiary-container/20 text-tertiary border-tertiary-container/30' },
  pending_info:       { label: 'Pending Info',     dot: 'bg-tertiary',              badge: 'bg-tertiary-container/20 text-tertiary border-tertiary-container/30' },
  approved:           { label: 'Approved',         dot: 'bg-secondary',             badge: 'bg-secondary-container/20 text-secondary border-secondary-container/30' },
  partially_approved: { label: 'Part. Approved',   dot: 'bg-secondary',             badge: 'bg-secondary-container/20 text-secondary border-secondary-container/30' },
  denied:             { label: 'Denied',           dot: 'bg-error',                 badge: 'bg-error-container/20 text-error border-error-container/30' },
  appealed:           { label: 'Appealed',         dot: 'bg-primary',               badge: 'bg-primary-container/20 text-primary border-primary-container/30' },
  closed:             { label: 'Closed',           dot: 'bg-outline',               badge: 'bg-surface-variant text-on-surface-variant border-outline-variant' },
};

// ── Component ─────────────────────────────────────────────────────────────────

export default function ClaimsPage() {
  const [claims,  setClaims]  = useState<Claim[]>([]);
  const [total,   setTotal]   = useState(0);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);
  const [filter,  setFilter]  = useState('');
  const [search,  setSearch]  = useState('');

  function loadClaims(status?: string) {
    setLoading(true);
    claimsAPI.list(1, status || undefined)
      .then((res) => { setClaims(res.items); setTotal(res.total); })
      .catch((e) => setError(String(e?.detail ?? e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => { loadClaims(filter || undefined); }, [filter]);

  const displayed = search
    ? claims.filter((c) =>
        c.claim_number.toLowerCase().includes(search.toLowerCase()) ||
        (c.ai_notes ?? '').toLowerCase().includes(search.toLowerCase())
      )
    : claims;

  return (
    <div className="h-full overflow-hidden relative p-8">
      {/* Mesh floating backgrounds */}
      <div className="absolute top-[10%] left-[5%] w-[400px] h-[400px] bg-primary/10 rounded-full blur-[100px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[10%] right-[10%] w-[500px] h-[500px] bg-tertiary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '2s' }} />

      <div className="max-w-[1440px] mx-auto space-y-8 relative z-10 h-full overflow-y-auto no-scrollbar">

        {/* ── Page Header ─────────────────────────────────────────────── */}
        <div>
          <h2 className="font-display text-[40px] text-on-surface leading-tight tracking-tight drop-shadow-md">Claims Center</h2>
          <p className="font-body-lg text-on-surface-variant mt-2">
            {total} total claims · Ingest, analyze, and adjudicate.
          </p>
        </div>

        {/* ── Ingestion Zone ──────────────────────────────────────────── */}
        <section className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center hover:border-primary/40 transition-all duration-300 cursor-pointer group relative overflow-hidden shadow-lg hover:shadow-[0_8px_32px_rgba(77,142,255,0.15)]">
          <div className="absolute inset-0 bg-primary/5 opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
          <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-4 group-hover:bg-primary/20 group-hover:border-primary/30 transition-all duration-300 z-10 shadow-inner">
            <span className="material-symbols-outlined text-on-surface group-hover:text-primary text-3xl transition-colors">
              cloud_upload
            </span>
          </div>
          <h3 className="font-headline-md text-[20px] text-on-surface z-10 tracking-tight">Claim Ingestion</h3>
          <p className="font-body-sm text-on-surface-variant mt-2 z-10">
            Upload Medical Invoices &amp; Prescriptions (PDF, PNG, JPG)
          </p>
          <p className="font-label-caps text-outline mt-2 z-10 bg-white/5 px-3 py-1 rounded-full border border-white/5">
            Drag and drop files here, or click to browse
          </p>
        </section>

        {/* ── Error ───────────────────────────────────────────────────── */}
        {error && (
          <div className="rounded-lg bg-error-container/20 border border-error-container/30 px-4 py-3">
            <p className="font-body-sm text-body-sm text-error">⚠ {error}</p>
          </div>
        )}

        {/* ── Processing Queue ─────────────────────────────────────────── */}
        <section className="glass-panel rounded-2xl overflow-hidden flex flex-col transition-all duration-300 shadow-xl">
          <div className="p-5 border-b border-white/5 flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-white/[0.02]">
            <h3 className="font-headline-md text-[18px] text-on-surface">Processing Queue</h3>
            <div className="flex items-center gap-3">
              {/* Search */}
              <div className="relative">
                <span className="material-symbols-outlined absolute left-3 top-2.5 text-on-surface-variant text-[18px]">search</span>
                <input
                  type="text"
                  placeholder="Filter by claim #..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="bg-surface/50 border border-white/10 rounded-xl py-2 pl-10 pr-4 text-sm text-on-surface focus:ring-2 focus:ring-primary/50 focus:border-primary/50 placeholder:text-on-surface-variant/50 w-full sm:w-64 font-body-sm backdrop-blur-md transition-all shadow-inner"
                />
              </div>

              {/* Status filter */}
              <div className="relative">
                <select
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  className="appearance-none bg-surface/50 border border-white/10 rounded-xl py-2 pl-4 pr-10 text-sm text-on-surface focus:ring-2 focus:ring-primary/50 focus:border-primary/50 font-body-sm backdrop-blur-md transition-all shadow-inner"
                >
                  <option value="">All Statuses</option>
                  <option value="submitted">Submitted</option>
                  <option value="in_review">In Review</option>
                  <option value="approved">Approved</option>
                  <option value="denied">Denied</option>
                </select>
                <span className="material-symbols-outlined absolute right-3 top-2.5 text-on-surface-variant pointer-events-none text-[18px]">expand_more</span>
              </div>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="bg-black/20 border-b border-white/5 font-label-caps text-on-surface-variant tracking-wider uppercase">
                  {['Claim ID', 'Billed', 'Allowed', 'AI Notes', 'Fraud Score', 'Status'].map((h) => (
                    <th key={h} className="py-4 px-6 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="font-body-sm divide-y divide-white/5">
                {loading
                  ? Array.from({ length: 4 }).map((_, i) => (
                      <tr key={i}>
                        {Array.from({ length: 6 }).map((_, j) => (
                          <td key={j} className="py-4 px-6">
                            <div className="h-4 rounded bg-surface-variant/40 animate-pulse w-3/4" />
                          </td>
                        ))}
                      </tr>
                    ))
                  : displayed.length === 0
                  ? (
                    <tr>
                      <td colSpan={6} className="py-8 text-center text-on-surface-variant font-body-sm text-body-sm">
                        No claims found.
                      </td>
                    </tr>
                  )
                  : displayed.map((row) => {
                      const cfg = STATUS_CONFIG[row.status] ?? STATUS_CONFIG.submitted;
                      return (
                        <tr key={row.id} className="hover:bg-white/[0.03] transition-colors group">
                          <td className="py-4 px-6 font-data-mono text-on-surface-variant group-hover:text-on-surface transition-colors">{row.claim_number}</td>
                          <td className="py-4 px-6 font-data-mono text-on-surface font-medium drop-shadow-sm">
                            ${parseFloat(row.billed_amount).toLocaleString()}
                          </td>
                          <td className="py-4 px-6 font-data-mono text-on-surface-variant">
                            {row.allowed_amount ? `$${parseFloat(row.allowed_amount).toLocaleString()}` : '—'}
                          </td>
                          <td className="py-4 px-6 text-on-surface-variant max-w-xs truncate">
                            {row.ai_notes ?? '—'}
                          </td>
                          <td className="py-4 px-6">
                            {row.fraud_score !== null ? (
                              <div className="flex items-center gap-1.5">
                                <div className="w-12 h-1 rounded-full bg-surface-variant overflow-hidden">
                                  <div
                                    className={`h-full ${row.fraud_score > 0.5 ? 'bg-error' : row.fraud_score > 0.2 ? 'bg-tertiary' : 'bg-secondary'}`}
                                    style={{ width: `${row.fraud_score * 100}%` }}
                                  />
                                </div>
                                <span className="font-data-mono text-data-mono text-on-surface-variant">
                                  {(row.fraud_score * 100).toFixed(0)}%
                                </span>
                              </div>
                            ) : (
                              <span className="text-on-surface-variant">—</span>
                            )}
                          </td>
                          <td className="py-4 px-6">
                            <span className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full font-label-caps tracking-wide border backdrop-blur-md ${cfg.badge}`}>
                              <span className={`w-1.5 h-1.5 rounded-full shadow-[0_0_8px_currentColor] ${cfg.dot}`} />
                              {cfg.label}
                            </span>
                          </td>
                        </tr>
                      );
                    })
                }
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
