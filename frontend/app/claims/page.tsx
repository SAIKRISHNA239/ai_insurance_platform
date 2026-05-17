'use client';

import { useEffect, useRef, useState } from 'react';
import { claimsAPI, type Claim, type ClaimIntakeResponse, type ClaimStatus } from '@/lib/api';

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

  // ── Ingestion state ───────────────────────────────────────────────
  const fileInputRef                                    = useRef<HTMLInputElement>(null);
  const [ingesting,     setIngesting]     = useState(false);
  const [ingestResult,  setIngestResult]  = useState<ClaimIntakeResponse | null>(null);
  const [ingestError,   setIngestError]   = useState<string | null>(null);
  const [isDragging,    setIsDragging]    = useState(false);

  // ── Adjudication state ────────────────────────────────────────────
  const [adjudicating,    setAdjudicating]    = useState<string | null>(null);
  const [adjudicateError, setAdjudicateError] = useState<string | null>(null);

  // ── Denial modal state ────────────────────────────────────────────
  const [denialModal, setDenialModal] = useState<{ claimId: string; claimNumber: string } | null>(null);
  const [denialReason, setDenialReason] = useState('');

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const file = files[0];

    const isJson = file.name.toLowerCase().endsWith('.json');
    const isBinary = /\.(pdf|png|jpe?g)$/i.test(file.name);

    if (!isJson && !isBinary) {
      setIngestError('Please upload a JSON (EDI 837) or image file (PDF, PNG, JPG).');
      return;
    }

    setIngesting(true);
    setIngestResult(null);
    setIngestError(null);

    try {
      let result: ClaimIntakeResponse;
      if (file.name.toLowerCase().endsWith('.json')) {
        // EDI 837 JSON — parse and send as JSON body
        const text    = await file.text();
        const payload = JSON.parse(text) as Record<string, unknown>;
        result = await claimsAPI.intakeClaim(payload);
      } else {
        // PDF / PNG / JPG — send as multipart/form-data
        result = await claimsAPI.uploadFile(file);
      }
      setIngestResult(result);
      loadClaims(filter || undefined); // refresh the queue
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'detail' in err) {
        const detail = (err as { detail: unknown }).detail;
        if (typeof detail === 'object' && detail !== null && 'violations' in detail) {
          // SNIP structured error
          const snip = detail as { error: string; violations: Array<{ tier: number; error_code: string; message: string }> };
          const lines = snip.violations.map(
            (v) => `Tier ${v.tier} [${v.error_code}]: ${v.message}`
          );
          setIngestError(`SNIP Validation Failed:\n${lines.join('\n')}`);
        } else {
          setIngestError(String(detail));
        }
      } else {
        setIngestError('Failed to parse or submit the claim file.');
      }
    } finally {
      setIngesting(false);
    }
  }

  function loadClaims(status?: string) {
    setLoading(true);
    claimsAPI.list(1, status || undefined)
      .then((res) => { setClaims(res.items); setTotal(res.total); })
      .catch((e) => setError(String(e?.detail ?? e)))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadClaims(filter || undefined); }, [filter]);

  async function handleAdjudicate(claimId: string, newStatus: 'approved' | 'denied', reason?: string) {
    setAdjudicating(claimId);
    setAdjudicateError(null);
    setDenialModal(null);
    setDenialReason('');
    try {
      await claimsAPI.updateStatus(claimId, newStatus as import('@/lib/api').ClaimStatus, reason);
      loadClaims(filter || undefined);
    } catch (err: unknown) {
      const e = err as { detail?: string };
      setAdjudicateError(`Failed to ${newStatus} claim: ${String(e?.detail ?? err)}`);
    } finally {
      setAdjudicating(null);
    }
  }

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

        {/* ── Ingestion Zone ─────────────────────────────────────────────────────── */}
        {/* Hidden native file input */}
        <input
          ref={fileInputRef}
          id="claim-file-input"
          type="file"
          accept=".json,.pdf,.png,.jpg,.jpeg,application/json,application/pdf,image/png,image/jpeg"
          className="sr-only"
          onChange={(e) => handleFiles(e.target.files)}
        />

        <section
          onClick={() => !ingesting && fileInputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={(e) => { e.preventDefault(); setIsDragging(false); handleFiles(e.dataTransfer.files); }}
          aria-label="Claim file ingestion drop zone"
          className={`glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center transition-all duration-300 cursor-pointer group relative overflow-hidden shadow-lg ${
            isDragging
              ? 'border-primary/60 bg-primary/10 shadow-[0_8px_32px_rgba(77,142,255,0.25)]'
              : ingesting
              ? 'border-white/10 cursor-wait'
              : 'hover:border-primary/40 hover:shadow-[0_8px_32px_rgba(77,142,255,0.15)]'
          }`}
        >
          <div className="absolute inset-0 bg-primary/5 opacity-0 group-hover:opacity-100 transition-opacity duration-300" />

          {/* Icon */}
          <div className={`w-16 h-16 rounded-2xl border flex items-center justify-center mb-4 transition-all duration-300 z-10 shadow-inner ${
            ingesting
              ? 'bg-primary/10 border-primary/30 animate-pulse'
              : isDragging
              ? 'bg-primary/30 border-primary/50'
              : 'bg-white/5 border-white/10 group-hover:bg-primary/20 group-hover:border-primary/30'
          }`}>
            <span className={`material-symbols-outlined text-3xl transition-colors z-10 ${
              ingesting ? 'text-primary animate-spin' :
              isDragging ? 'text-primary' :
              'text-on-surface group-hover:text-primary'
            }`}>
              {ingesting ? 'sync' : isDragging ? 'file_download' : 'cloud_upload'}
            </span>
          </div>

          <h3 className="font-headline-md text-[20px] text-on-surface z-10 tracking-tight">
            {ingesting ? 'Processing EDI Claim…' : 'Claim Ingestion'}
          </h3>
          <p className="font-body-sm text-on-surface-variant mt-2 z-10">
            {ingesting
              ? 'Running SNIP 7-tier validation pipeline…'
              : 'Drop an EDI 837 JSON, PDF, or image file here — or click to browse'}
          </p>
          {!ingesting && (
            <p className="font-label-caps text-outline mt-2 z-10 bg-white/5 px-3 py-1 rounded-full border border-white/5">
              .json &nbsp;&bull;&nbsp; .pdf &nbsp;&bull;&nbsp; .png &nbsp;&bull;&nbsp; .jpg
            </p>
          )}
        </section>

        {/* Ingestion success banner */}
        {ingestResult && (
          <div className="rounded-xl bg-secondary-container/20 border border-secondary-container/40 px-5 py-4 flex items-start gap-4 animate-in fade-in duration-300">
            <span className="material-symbols-outlined text-secondary text-[24px] shrink-0 mt-0.5">check_circle</span>
            <div className="flex-1 min-w-0">
              <p className="font-body-sm font-semibold text-secondary">
                Claim Accepted — {ingestResult.claim_number}
              </p>
              <p className="font-body-sm text-on-surface-variant mt-0.5 text-sm">
                {ingestResult.message}
              </p>
              <p className="font-label-caps text-on-surface-variant/60 mt-1">
                State: {ingestResult.adjudication_state} &nbsp;&bull;&nbsp; UM Route: {ingestResult.um_route ?? 'processing…'}
              </p>
            </div>
            <button
              onClick={() => setIngestResult(null)}
              className="shrink-0 text-on-surface-variant hover:text-on-surface transition-colors"
              aria-label="Dismiss"
            >
              <span className="material-symbols-outlined text-[18px]">close</span>
            </button>
          </div>
        )}

        {/* Ingestion error banner */}
        {ingestError && (
          <div className="rounded-xl bg-error-container/20 border border-error-container/40 px-5 py-4 flex items-start gap-4 animate-in fade-in duration-300">
            <span className="material-symbols-outlined text-error text-[24px] shrink-0 mt-0.5">error</span>
            <div className="flex-1 min-w-0">
              <p className="font-body-sm font-semibold text-error">Ingestion Failed</p>
              <pre className="font-body-sm text-on-surface-variant mt-0.5 text-sm whitespace-pre-wrap break-words">{ingestError}</pre>
            </div>
            <button
              onClick={() => setIngestError(null)}
              className="shrink-0 text-on-surface-variant hover:text-on-surface transition-colors"
              aria-label="Dismiss"
            >
              <span className="material-symbols-outlined text-[18px]">close</span>
            </button>
          </div>
        )}

        {/* ── Error ───────────────────────────────────────────────────── */}
        {error && (
          <div className="rounded-lg bg-error-container/20 border border-error-container/30 px-4 py-3">
            <p className="font-body-sm text-body-sm text-error">⚠ {error}</p>
          </div>
        )}

        {/* Adjudication error banner */}
        {adjudicateError && (
          <div className="rounded-xl bg-error-container/20 border border-error-container/40 px-5 py-4 flex items-center justify-between gap-4 animate-in fade-in duration-200">
            <div className="flex items-center gap-3">
              <span className="material-symbols-outlined text-error text-[18px]">gavel</span>
              <p className="font-body-sm text-error text-sm">{adjudicateError}</p>
            </div>
            <button onClick={() => setAdjudicateError(null)} className="text-on-surface-variant hover:text-on-surface transition-colors">
              <span className="material-symbols-outlined text-[18px]">close</span>
            </button>
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
                  {['Claim ID', 'Billed', 'Allowed', 'AI Notes', 'Fraud Score', 'Status', 'Actions'].map((h) => (
                    <th key={h} className="py-4 px-6 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="font-body-sm divide-y divide-white/5">
                {loading
                  ? Array.from({ length: 4 }).map((_, i) => (
                      <tr key={i}>
                        {Array.from({ length: 7 }).map((_, j) => (
                          <td key={j} className="py-4 px-6">
                            <div className="h-4 rounded bg-surface-variant/40 animate-pulse w-3/4" />
                          </td>
                        ))}
                      </tr>
                    ))
                  : displayed.length === 0
                  ? (
                    <tr>
                      <td colSpan={7} className="py-8 text-center text-on-surface-variant font-body-sm text-body-sm">
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

                          {/* Actions cell */}
                          <td className="py-4 px-6">
                            {['submitted', 'in_review', 'pending_info', 'appealed'].includes(row.status) ? (
                              <div className="flex items-center gap-2">
                                <button
                                  id={`approve-claim-${row.id}`}
                                  disabled={adjudicating === row.id}
                                  onClick={() => handleAdjudicate(row.id, 'approved')}
                                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-secondary/15 border border-secondary/25 text-secondary font-label-caps text-[11px] hover:bg-secondary hover:text-on-primary transition-all duration-150 disabled:opacity-40 disabled:cursor-wait"
                                >
                                  {adjudicating === row.id ? (
                                    <span className="material-symbols-outlined text-[14px] animate-spin">sync</span>
                                  ) : (
                                    <span className="material-symbols-outlined text-[14px]">check_circle</span>
                                  )}
                                  Approve
                                </button>
                                <button
                                  id={`deny-claim-${row.id}`}
                                  disabled={adjudicating === row.id}
                                  onClick={() => setDenialModal({ claimId: row.id, claimNumber: row.claim_number })}
                                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-error/15 border border-error/25 text-error font-label-caps text-[11px] hover:bg-error hover:text-on-primary transition-all duration-150 disabled:opacity-40 disabled:cursor-wait"
                                >
                                  <span className="material-symbols-outlined text-[14px]">cancel</span>
                                  Deny
                                </button>
                              </div>
                            ) : (
                              <span className="text-on-surface-variant/30 font-label-caps text-[11px]">—</span>
                            )}
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

      {/* ── Denial Reason Modal ─────────────────────────────────────── */}
      {denialModal && (
        <div
          id="denial-modal-overlay"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) { setDenialModal(null); setDenialReason(''); } }}
          onKeyDown={(e) => { if (e.key === 'Escape') { setDenialModal(null); setDenialReason(''); } }}
        >
          <div
            id="denial-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="denial-modal-title"
            className="relative w-full max-w-md mx-4 rounded-2xl bg-[#12182b] border border-white/10 shadow-2xl p-6 space-y-5 animate-in fade-in zoom-in-95 duration-200"
          >
            {/* Header */}
            <div className="flex items-start justify-between gap-4">
              <div className="flex items-center gap-3">
                <span className="material-symbols-outlined text-error text-[22px]">gavel</span>
                <div>
                  <h2 id="denial-modal-title" className="font-semibold text-white text-base">Deny Claim</h2>
                  <p className="text-white/40 text-xs mt-0.5">{denialModal.claimNumber}</p>
                </div>
              </div>
              <button
                onClick={() => { setDenialModal(null); setDenialReason(''); }}
                className="text-white/30 hover:text-white transition-colors mt-0.5"
                aria-label="Close"
              >
                <span className="material-symbols-outlined text-[20px]">close</span>
              </button>
            </div>

            {/* Reason field */}
            <div className="space-y-2">
              <label htmlFor="denial-reason-input" className="block text-white/60 text-sm font-medium">
                Denial Reason <span className="text-error">*</span>
              </label>
              <textarea
                id="denial-reason-input"
                rows={4}
                value={denialReason}
                onChange={(e) => setDenialReason(e.target.value)}
                placeholder="e.g. Not medically necessary per policy section 4.2 — CPT 99214 not covered without referral…"
                className="w-full rounded-xl bg-white/5 border border-white/10 px-4 py-3 text-sm text-white placeholder:text-white/25 focus:outline-none focus:border-error/50 focus:ring-1 focus:ring-error/30 resize-none transition-colors"
              />
              <p className="text-white/30 text-xs">{denialReason.length} chars — include policy section if applicable</p>
            </div>

            {/* Actions */}
            <div className="flex items-center justify-end gap-3 pt-1">
              <button
                onClick={() => { setDenialModal(null); setDenialReason(''); }}
                className="px-4 py-2 rounded-xl text-sm text-white/60 hover:text-white transition-colors"
              >
                Cancel
              </button>
              <button
                id="confirm-denial-btn"
                disabled={!denialReason.trim() || adjudicating === denialModal.claimId}
                onClick={() => handleAdjudicate(denialModal.claimId, 'denied', denialReason.trim())}
                className="inline-flex items-center gap-2 px-5 py-2 rounded-xl bg-error text-on-primary text-sm font-semibold hover:bg-error/80 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {adjudicating === denialModal.claimId ? (
                  <span className="material-symbols-outlined text-[16px] animate-spin">sync</span>
                ) : (
                  <span className="material-symbols-outlined text-[16px]">gavel</span>
                )}
                Confirm Denial
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
