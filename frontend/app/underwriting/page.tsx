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
        w-full text-left px-3 py-3 rounded-lg border transition-all group
        ${isSelected
          ? 'bg-secondary/10 border-secondary/30 shadow-sm shadow-secondary/10'
          : 'bg-surface-container border-outline-variant hover:bg-surface-container-high hover:border-outline'}
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

  // Load application queue
  useEffect(() => {
    setIsLoadingApps(true);
    applicationsAPI.list()
      .then((res) => setApplications(res.items))
      .catch(console.error)
      .finally(() => setIsLoadingApps(false));
  }, []);

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

  const handleAction = useCallback((action: 'approve' | 'decline' | 'postpone') => {
    if (!selectedApp) return;
    const labels = { approve: 'Approved ✓', decline: 'Declined ✗', postpone: 'Postponed ⏸' };
    setActionFeedback(`${labels[action]} — ${selectedApp.application_number}`);
    setTimeout(() => setActionFeedback(null), 4000);
  }, [selectedApp]);

  return (
    <div className="h-full flex overflow-hidden bg-background">

      {/* ── Application Queue Sidebar ──────────────────────────────────── */}
      <aside className="w-64 shrink-0 border-r border-outline-variant bg-surface-container-low flex flex-col">
        <div className="px-4 pt-4 pb-3 border-b border-outline-variant">
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
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">

        {/* Application sub-header */}
        {selectedApp && (
          <div className="shrink-0 flex items-center justify-between px-6 py-3 border-b border-outline-variant bg-surface-container-lowest">
            <div>
              <h1 className="font-headline-md text-headline-md text-on-surface">{selectedApp.application_number}</h1>
              <p className="font-body-sm text-body-sm text-on-surface-variant">
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
              <section className="w-1/2 flex flex-col border-r border-outline-variant bg-surface min-w-[380px] overflow-hidden">
                <GenAIAssistant
                  applicationId={selectedApp.id}
                  application={selectedApp}
                  onCitationClick={handleCitationClick}
                  className="flex-1 min-h-0"
                />

                {/* Decision action bar — always visible when app selected */}
                <div className="shrink-0 px-6 py-4 border-t border-outline-variant bg-surface-container-lowest flex gap-3">
                  <button
                    onClick={() => handleAction('approve')}
                    className="flex-1 bg-primary/20 border border-primary/40 text-primary font-body-sm text-body-sm font-semibold py-2.5 px-4 rounded-lg hover:bg-primary hover:text-on-primary transition-all focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-surface-container-lowest"
                  >
                    Approve &amp; Issue
                  </button>
                  <button
                    onClick={() => handleAction('decline')}
                    className="flex-1 bg-error-container/20 border border-error-container/40 text-error font-body-sm text-body-sm font-semibold py-2.5 px-4 rounded-lg hover:bg-error-container hover:text-on-error-container transition-all"
                  >
                    Decline
                  </button>
                  <button
                    onClick={() => handleAction('postpone')}
                    className="flex-1 border border-outline-variant bg-transparent text-on-surface font-body-sm text-body-sm font-semibold py-2.5 px-4 rounded-lg hover:bg-surface-variant transition-colors"
                  >
                    Postpone
                  </button>
                </div>
              </section>

              {/* Right: Citation / document viewer */}
              <section className="w-1/2 flex flex-col bg-surface-container-lowest overflow-hidden">
                <CitationViewer activeCitation={activeCitation} className="flex-1 min-h-0" />
              </section>
            </>
          ) : (
            /* Empty state */
            <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center p-8">
              <div className="w-16 h-16 rounded-2xl bg-surface-container border border-outline-variant flex items-center justify-center text-on-surface-variant">
                <span className="material-symbols-outlined text-3xl">description</span>
              </div>
              <div>
                <p className="font-headline-md text-headline-md text-on-surface">Select an application</p>
                <p className="font-body-sm text-body-sm text-on-surface-variant mt-1 max-w-xs">
                  Choose a case from the review queue to launch the AI underwriting analysis.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Score card — below the split (only when app selected) */}
        {selectedApp && (
          <div className="shrink-0 px-6 py-4 border-t border-outline-variant bg-surface-container-lowest">
            {isLoadingDecision
              ? <div className="h-20 rounded-xl bg-surface-container animate-pulse" />
              : <ScoreCard decision={decision} />
            }
          </div>
        )}
      </div>
    </div>
  );
}
