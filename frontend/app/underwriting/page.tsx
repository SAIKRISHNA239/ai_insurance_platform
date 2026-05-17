'use client';

/**
 * app/underwriting/page.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * HITL Underwriting Desk — full-height split workspace.
 *
 * LAYOUT (within the global shell from layout.tsx):
 * ┌───────────────────────────────────────────────────────────┐
 * │ Application Queue (aside, w-64) │ GenAI Assistant  │ Doc  │
 * │  ← page-specific sidebar →      │ (left 50%)       │ View │
 * │                                  │                  │ (50%)│
 * │                                  ├──────────────────┴──────┤
 * │                                  │ Score Card + Action Bar │
 * └───────────────────────────────────────────────────────────┘
 *
 * STATE FLOW
 * ──────────
 * 1. Queue loads → user selects an Application
 * 2. GenAIAssistant auto-runs streaming analysis
 * 3. Citation click → activeCitation state → CitationViewer highlights
 * 4. Underwriter clicks Approve / Decline / Postpone
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  type Application,
  type ApplicationStatus,
  type Citation,
  type UnderwritingDecision,
  type UnderwritingRoute,
  applicationsAPI,
} from '@/lib/api';
import GenAIAssistant from '@/components/GenAIAssistant';
import CitationViewer from '@/components/ui/CitationViewer';

// ── Score Card ────────────────────────────────────────────────────────────────

function ScoreCard({ decision }: { decision: UnderwritingDecision | null }) {
  if (!decision) return null;

  const routeConfig: Record<UnderwritingRoute, { label: string; color: string; bg: string; border: string }> = {
    stp_approved:        { label: 'STP Approved',      color: 'text-secondary', bg: 'bg-secondary-container/20', border: 'border-secondary-container/40' },
    conditional_approved:{ label: 'Conditional Issue', color: 'text-tertiary',  bg: 'bg-tertiary-container/20',  border: 'border-tertiary-container/40' },
    manual_review:       { label: 'Manual Review',     color: 'text-error',     bg: 'bg-error-container/20',     border: 'border-error-container/40' },
  };

  const cfg = routeConfig[decision.route];

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {[
        { label: 'Net Score',    value: `${decision.net_score} pts` },
        { label: 'Table Rating', value: decision.table_rating === 0 ? 'Standard' : `Table ${decision.table_rating}` },
        { label: 'Premium',      value: decision.suggested_premium ? `$${decision.suggested_premium}/mo` : 'TBD' },
        { label: 'Risk Tier',    value: decision.risk_tier.charAt(0).toUpperCase() + decision.risk_tier.slice(1) },
      ].map((stat) => (
        <div key={stat.label} className="rounded-lg bg-surface-container border border-outline-variant px-4 py-3">
          <p className="font-label-caps text-label-caps text-on-surface-variant uppercase mb-1">{stat.label}</p>
          <p className="font-headline-md text-headline-md text-on-surface">{stat.value}</p>
        </div>
      ))}

      <div className={`col-span-2 sm:col-span-4 rounded-lg ${cfg.bg} border ${cfg.border} px-4 py-3`}>
        <p className="font-label-caps text-label-caps text-on-surface-variant uppercase mb-0.5">AI Routing Decision</p>
        <p className={`font-body-sm text-body-sm font-semibold ${cfg.color}`}>{cfg.label}</p>
        <p className="font-body-sm text-body-sm text-on-surface-variant mt-0.5">{decision.routing_reason}</p>
      </div>

      {decision.permanent_exclusions.length > 0 && (
        <div className="col-span-2 sm:col-span-4 rounded-lg bg-tertiary-container/20 border border-tertiary-container/40 px-4 py-3">
          <p className="font-label-caps text-label-caps text-on-surface-variant uppercase mb-1">Exclusion Riders</p>
          <ul className="space-y-0.5">
            {decision.permanent_exclusions.map((excl) => (
              <li key={excl} className="font-body-sm text-body-sm text-tertiary flex items-center gap-1.5">
                <span className="w-1 h-1 rounded-full bg-tertiary flex-shrink-0" />
                {excl}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── Application Queue Row ─────────────────────────────────────────────────────

interface ApplicationRowProps {
  app: Application;
  isSelected: boolean;
  onClick: () => void;
}

function ApplicationRow({ app, isSelected, onClick }: ApplicationRowProps) {
  const tierBadge: Record<string, string> = {
    preferred:   'bg-secondary-container/20 text-secondary',
    standard:    'bg-primary-container/20 text-primary',
    substandard: 'bg-tertiary-container/20 text-tertiary',
    decline:     'bg-error-container/20 text-error',
  };

  return (
    <button
      onClick={onClick}
      className={`
        w-full text-left px-4 py-3 rounded-xl border transition-all duration-300 group
        ${isSelected
          ? 'glass-panel border-secondary/40 shadow-[0_0_16px_rgba(40,167,69,0.15)] bg-secondary/5'
          : 'glass-card hover:bg-white/5 border-transparent hover:border-white/10'}
      `}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className={`font-body-sm text-body-sm font-semibold truncate ${isSelected ? 'text-secondary' : 'text-on-surface'}`}>
            {app.application_number}
          </p>
          <p className="font-label-caps text-label-caps text-on-surface-variant mt-0.5 capitalize">{app.policy_type}</p>
        </div>
        {app.risk_tier && (
          <span className={`flex-shrink-0 font-label-caps text-label-caps px-1.5 py-0.5 rounded-full ${tierBadge[app.risk_tier] ?? 'bg-surface-variant text-on-surface-variant'}`}>
            {app.risk_tier.toUpperCase()}
          </span>
        )}
      </div>
      {app.underwriting_score !== null && (
        <div className="mt-2 flex items-center gap-2">
          <div className="flex-1 h-1 rounded-full bg-surface-variant overflow-hidden">
            <div
              className="h-full rounded-full bg-gradient-to-r from-secondary to-error transition-all"
              style={{ width: `${Math.min(100, app.underwriting_score)}%` }}
            />
          </div>
          <span className="font-data-mono text-data-mono text-on-surface-variant tabular-nums">
            {app.underwriting_score}pts
          </span>
        </div>
      )}
    </button>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function UnderwritingPage() {
  const [applications, setApplications]     = useState<Application[]>([]);
  const [selectedApp, setSelectedApp]       = useState<Application | null>(null);
  const [decision, setDecision]             = useState<UnderwritingDecision | null>(null);
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const [isLoadingApps, setIsLoadingApps]   = useState(true);
  const [isLoadingDecision, setIsLoadingDecision] = useState(false);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting]     = useState(false);

  // ── Load application queue (extracted so it can be called after a decision) ──
  const loadApplications = useCallback(() => {
    setIsLoadingApps(true);
    applicationsAPI.list()
      .then((res) => setApplications(res.items))
      .catch(console.error)
      .finally(() => setIsLoadingApps(false));
  }, []);

  useEffect(() => { loadApplications(); }, [loadApplications]);

  // Auto-select the first application once the queue is loaded
  useEffect(() => {
    if (applications.length > 0 && !selectedApp) {
      setSelectedApp(applications[0]);
    }
  }, [applications]); // eslint-disable-line react-hooks/exhaustive-deps

  // Load decision when app selected
  useEffect(() => {
    if (!selectedApp) { setDecision(null); return; }
    setIsLoadingDecision(true);
    setActiveCitation(null);
    applicationsAPI.getDecision(selectedApp.id)
      .then(setDecision)
      .catch(() => setDecision(null))
      .finally(() => setIsLoadingDecision(false));
  }, [selectedApp]);

  const handleCitationClick = useCallback((citation: Citation) => {
    setActiveCitation(citation);
  }, []);

  // ── Action → ApplicationStatus mapping ────────────────────────────────────
  const ACTION_STATUS_MAP: Record<'approve' | 'decline' | 'postpone', ApplicationStatus> = {
    approve:  'approved',
    decline:  'declined',
    postpone: 'under_review',
  };

  const handleAction = useCallback(async (action: 'approve' | 'decline' | 'postpone') => {
    if (!selectedApp || isSubmitting) return;

    const labels = { approve: 'Approved ✓', decline: 'Declined ✗', postpone: 'Postponed ⏸' };
    setIsSubmitting(true);

    try {
      await applicationsAPI.submitDecision(selectedApp.id, {
        status: ACTION_STATUS_MAP[action],
      });
      // Show success toast
      setActionFeedback(`${labels[action]} — ${selectedApp.application_number}`);
      // Deselect the decided application and refresh the queue
      setSelectedApp(null);
      loadApplications();
    } catch (err: unknown) {
      const detail =
        err && typeof err === 'object' && 'detail' in err
          ? String((err as { detail: unknown }).detail)
          : 'An unexpected error occurred.';
      setActionFeedback(`⚠ Error: ${detail}`);
    } finally {
      setIsSubmitting(false);
      setTimeout(() => setActionFeedback(null), 5000);
    }
  }, [selectedApp, isSubmitting, loadApplications]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="h-full flex overflow-hidden relative">
      {/* Mesh floating backgrounds */}
      <div className="absolute top-[5%] left-[5%] w-[500px] h-[500px] bg-primary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[20%] right-[10%] w-[400px] h-[400px] bg-secondary/10 rounded-full blur-[100px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '3s' }} />

      {/* ── Application Queue Sidebar ──────────────────────────────────── */}
      <aside className="w-72 shrink-0 border-r border-white/10 glass-panel flex flex-col z-10 relative shadow-xl">
        <div className="px-5 pt-5 pb-4 border-b border-white/5 bg-black/20">
          <h2 className="font-label-caps text-label-caps text-on-surface uppercase tracking-widest">Review Queue</h2>
          <p className="font-label-caps text-label-caps text-on-surface-variant mt-0.5">
            {applications.length} applications
          </p>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {isLoadingApps
            ? Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="h-16 rounded-lg bg-surface-container animate-pulse" />
              ))
            : applications.length === 0
            ? <p className="font-body-sm text-body-sm text-on-surface-variant text-center py-6">No applications in queue.</p>
            : applications.map((app) => (
                <ApplicationRow
                  key={app.id}
                  app={app}
                  isSelected={selectedApp?.id === app.id}
                  onClick={() => setSelectedApp(app)}
                />
              ))
          }
        </div>
      </aside>

      {/* ── Main Workspace ────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden relative z-10 backdrop-blur-sm">

        {/* Application sub-header */}
        {selectedApp && (
          <div className="shrink-0 flex items-center justify-between px-8 py-4 border-b border-white/10 bg-white/[0.02]">
            <div>
              <h1 className="font-display text-[28px] text-on-surface drop-shadow-sm">{selectedApp.application_number}</h1>
              <p className="font-data-mono text-on-surface-variant mt-1">
                {selectedApp.policy_type} · submitted {new Date(selectedApp.created_at).toLocaleDateString()}
              </p>
            </div>
            {actionFeedback && (
              <div className="px-3 py-1.5 rounded-lg bg-surface-container border border-outline-variant font-body-sm text-body-sm text-on-surface animate-in fade-in duration-200">
                {actionFeedback}
              </div>
            )}
          </div>
        )}

        {/* Split workspace */}
        <div className="flex-1 flex min-h-0 overflow-hidden">
          {selectedApp ? (
            <>
              {/* Left: GenAI assistant panel */}
              <section className="w-1/2 flex flex-col border-r border-white/10 glass-card min-w-[420px] overflow-hidden">
                <GenAIAssistant
                  applicationId={selectedApp.id}
                  application={selectedApp}
                  onCitationClick={handleCitationClick}
                  className="flex-1 min-h-0"
                />

                {/* Decision action bar — always visible when app selected */}
                <div className="shrink-0 px-6 py-5 border-t border-white/10 bg-black/20 flex gap-4 backdrop-blur-md">
                  <button
                    onClick={() => handleAction('approve')}
                    disabled={isSubmitting}
                    className="flex-1 bg-primary/20 border border-primary/40 text-primary font-label-caps tracking-wider py-3 px-4 rounded-xl hover:bg-primary hover:text-on-primary hover:shadow-[0_0_16px_rgba(77,142,255,0.4)] transition-all focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-black disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-primary/20 disabled:hover:text-primary"
                  >
                    {isSubmitting ? 'Submitting…' : 'Approve & Issue'}
                  </button>
                  <button
                    onClick={() => handleAction('decline')}
                    disabled={isSubmitting}
                    className="flex-1 bg-error-container/20 border border-error-container/40 text-error font-label-caps tracking-wider py-3 px-4 rounded-xl hover:bg-error hover:text-on-error hover:shadow-[0_0_16px_rgba(255,84,73,0.4)] transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Decline
                  </button>
                  <button
                    onClick={() => handleAction('postpone')}
                    disabled={isSubmitting}
                    className="flex-1 border border-white/20 bg-white/5 text-on-surface font-label-caps tracking-wider py-3 px-4 rounded-xl hover:bg-white/10 hover:border-white/30 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Postpone
                  </button>
                </div>
              </section>

              {/* Right: Citation / document viewer */}
              <section className="w-1/2 flex flex-col bg-white/[0.02] overflow-hidden">
                <CitationViewer activeCitation={activeCitation} className="flex-1 min-h-0" />
              </section>
            </>
          ) : (
            /* Empty state */
            <div className="flex-1 flex flex-col items-center justify-center gap-5 text-center p-8">
              <div className="w-20 h-20 rounded-3xl glass-card border border-white/10 flex items-center justify-center text-on-surface-variant shadow-inner">
                <span className="material-symbols-outlined text-[40px] opacity-70">description</span>
              </div>
              <div>
                <p className="font-display text-[28px] text-on-surface">Select an application</p>
                <p className="font-body-sm text-body-sm text-on-surface-variant mt-1 max-w-xs">
                  Choose a case from the review queue to launch the AI underwriting analysis.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Score card — below the split (only when app selected) */}
        {selectedApp && (
          <div className="shrink-0 px-8 py-5 border-t border-white/10 bg-black/20 backdrop-blur-md">
            {isLoadingDecision
              ? <div className="h-20 rounded-2xl bg-white/5 animate-pulse" />
              : <ScoreCard decision={decision} />
            }
          </div>
        )}
      </div>
    </div>
  );
}
