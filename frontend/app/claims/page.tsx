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
    <div className="h-full overflow-y-auto p-8 bg-background">
      <div className="max-w-[1440px] mx-auto space-y-6">

        {/* ── Page Header ─────────────────────────────────────────────── */}
        <div>
          <h2 className="font-display text-display text-on-surface">Claims Center</h2>
          <p className="font-body-lg text-body-lg text-on-surface-variant mt-1">
            {total} total claims · Ingest, analyze, and adjudicate.
          </p>
        </div>

        {/* ── Ingestion Zone ──────────────────────────────────────────── */}
        <section className="bg-surface-container-low border border-outline-variant rounded-xl p-8 flex flex-col items-center justify-center text-center hover:border-primary/50 transition-colors cursor-pointer group relative overflow-hidden">
          <div className="absolute inset-0 bg-primary/5 opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
          <div className="w-16 h-16 rounded-full bg-surface-variant flex items-center justify-center mb-4 group-hover:bg-primary-container transition-colors z-10">
            <span className="material-symbols-outlined text-on-surface group-hover:text-on-primary-container text-3xl transition-colors">
              cloud_upload
            </span>
          </div>
          <h3 className="font-headline-md text-headline-md text-on-surface z-10">Claim Ingestion</h3>
          <p className="font-body-sm text-body-sm text-on-surface-variant mt-2 z-10">
            Upload Medical Invoices &amp; Prescriptions (PDF, PNG, JPG)
          </p>
          <p className="font-body-sm text-body-sm text-outline mt-1 z-10">
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
        <section className="bg-surface-container-low border border-outline-variant rounded-xl overflow-hidden flex flex-col">
          <div className="p-4 border-b border-outline-variant flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-surface-container">
            <h3 className="font-headline-md text-headline-md text-on-surface">Processing Queue</h3>
            <div className="flex items-center gap-3">
              {/* Search */}
              <div className="relative">
                <span className="material-symbols-outlined absolute left-2.5 top-2 text-outline text-sm">search</span>
                <input
                  type="text"
                  placeholder="Filter by claim #..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="bg-surface border border-outline-variant rounded-lg py-1.5 pl-8 pr-3 text-sm text-on-surface focus:ring-1 focus:ring-primary focus:border-primary placeholder:text-on-surface-variant w-full sm:w-56 font-body-sm"
                />
              </div>

              {/* Status filter */}
              <div className="relative">
                <select
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  className="appearance-none bg-surface border border-outline-variant rounded-lg py-1.5 pl-3 pr-8 text-sm text-on-surface focus:ring-1 focus:ring-primary font-body-sm"
                >
                  <option value="">All Statuses</option>
                  <option value="submitted">Submitted</option>
                  <option value="in_review">In Review</option>
                  <option value="approved">Approved</option>
                  <option value="denied">Denied</option>
                </select>
                <span className="material-symbols-outlined absolute right-2 top-1.5 text-on-surface-variant pointer-events-none text-sm">expand_more</span>
              </div>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-outline-variant bg-surface/50 font-label-caps text-label-caps text-on-surface-variant uppercase">
                  {['Claim ID', 'Billed', 'Allowed', 'AI Notes', 'Fraud Score', 'Status'].map((h) => (
                    <th key={h} className="py-3 px-4 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="font-body-sm text-body-sm divide-y divide-outline-variant/50">
                {loading
                  ? Array.from({ length: 4 }).map((_, i) => (
                      <tr key={i}>
                        {Array.from({ length: 6 }).map((_, j) => (
                          <td key={j} className="py-3 px-4">
                            <div className="h-3 rounded bg-surface-container animate-pulse w-3/4" />
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
                        <tr key={row.id} className="hover:bg-surface-variant/30 transition-colors">
                          <td className="py-3 px-4 font-data-mono text-data-mono text-on-surface-variant">{row.claim_number}</td>
                          <td className="py-3 px-4 font-data-mono text-data-mono text-on-surface">
                            ${parseFloat(row.billed_amount).toLocaleString()}
                          </td>
                          <td className="py-3 px-4 font-data-mono text-data-mono text-on-surface-variant">
                            {row.allowed_amount ? `$${parseFloat(row.allowed_amount).toLocaleString()}` : '—'}
                          </td>
                          <td className="py-3 px-4 text-on-surface-variant max-w-xs truncate">
                            {row.ai_notes ?? '—'}
                          </td>
                          <td className="py-3 px-4">
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
                          <td className="py-3 px-4">
                            <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold tracking-wide border ${cfg.badge}`}>
                              <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
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
