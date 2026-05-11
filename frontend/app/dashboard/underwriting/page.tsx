"use client";

/**
 * app/dashboard/underwriting/page.tsx
 * ──────────────────────────────────────────────────────────────────────────────
 * HITL Underwriting Dashboard — main layout page.
 *
 * LAYOUT ARCHITECTURE
 * ─────────────────────
 * ┌─────────────────────────────────────────────────────────────────┐
 * │  Top Nav Bar                                                    │
 * ├──────────────────┬──────────────────────────────────────────────┤
 * │                  │                                              │
 * │  Application     │  Two-column HITL workspace                  │
 * │  Queue           │  ┌───────────────┬──────────────────────┐   │
 * │  (left sidebar)  │  │ GenAI         │ CitationViewer       │   │
 * │                  │  │ Assistant     │ (PDF + bounding box) │   │
 * │                  │  └───────────────┴──────────────────────┘   │
 * │                  │  Decision Panel (score card + action bar)    │
 * ├──────────────────┴──────────────────────────────────────────────┤
 * └─────────────────────────────────────────────────────────────────┘
 *
 * STATE FLOW
 * ───────────
 * 1. User selects application from queue → sets `selectedApplication`.
 * 2. GenAIAssistant auto-triggers streaming analysis.
 * 3. User clicks citation badge → sets `activeCitation`.
 * 4. CitationViewer jumps to the cited PDF page and draws highlight.
 * 5. Underwriter approves/declines via the Decision Panel action buttons.
 */

import React, { useCallback, useEffect, useState } from "react";
import {
  type Application,
  type Citation,
  type UnderwritingDecision,
  type UnderwritingRoute,
  applicationsAPI,
} from "@/lib/api";
import GenAIAssistant from "@/components/GenAIAssistant";
import CitationViewer from "@/components/ui/CitationViewer";

// ─── Score Card ────────────────────────────────────────────────────────────────

function ScoreCard({ decision }: { decision: UnderwritingDecision | null }) {
  if (!decision) return null;

  const routeConfigMap: Record<UnderwritingRoute, { label: string; color: string; bg: string; border: string }> = {
    stp_approved: {
      label: "STP Approved",
      color: "text-emerald-400",
      bg: "bg-emerald-500/10",
      border: "border-emerald-500/30",
    },
    conditional_approved: {
      label: "Conditional Issue",
      color: "text-amber-400",
      bg: "bg-amber-500/10",
      border: "border-amber-500/30",
    },
    manual_review: {
      label: "Manual Review",
      color: "text-red-400",
      bg: "bg-red-500/10",
      border: "border-red-500/30",
    },
  };

  const routeConfig = routeConfigMap[decision.route];

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {[
        { label: "Net Score", value: `${decision.net_score} pts` },
        { label: "Table Rating", value: decision.table_rating === 0 ? "Standard" : `Table ${decision.table_rating}` },
        { label: "Premium", value: decision.suggested_premium ? `$${decision.suggested_premium}/mo` : "TBD" },
        { label: "Risk Tier", value: decision.risk_tier.charAt(0).toUpperCase() + decision.risk_tier.slice(1) },
      ].map((stat) => (
        <div
          key={stat.label}
          className="rounded-lg bg-slate-800/60 border border-slate-700/40 px-4 py-3"
        >
          <p className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">{stat.label}</p>
          <p className="text-base font-semibold text-slate-100">{stat.value}</p>
        </div>
      ))}

      <div className={`col-span-2 sm:col-span-4 rounded-lg ${routeConfig.bg} border ${routeConfig.border} px-4 py-3`}>
        <p className="text-[10px] text-slate-500 uppercase tracking-widest mb-0.5">AI Routing Decision</p>
        <p className={`text-sm font-semibold ${routeConfig.color}`}>{routeConfig.label}</p>
        <p className="text-xs text-slate-400 mt-0.5">{decision.routing_reason}</p>
      </div>

      {decision.permanent_exclusions.length > 0 && (
        <div className="col-span-2 sm:col-span-4 rounded-lg bg-orange-500/10 border border-orange-500/30 px-4 py-3">
          <p className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Exclusion Riders</p>
          <ul className="space-y-0.5">
            {decision.permanent_exclusions.map((excl) => (
              <li key={excl} className="text-xs text-orange-300 flex items-center gap-1.5">
                <span className="w-1 h-1 rounded-full bg-orange-400 flex-shrink-0" />
                {excl}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ─── Application Queue Row ─────────────────────────────────────────────────────

interface ApplicationRowProps {
  app: Application;
  isSelected: boolean;
  onClick: () => void;
}

function ApplicationRow({
  app,
  isSelected,
  onClick,
}: ApplicationRowProps) {
  const tierColors: Record<string, string> = {
    preferred: "bg-emerald-500/20 text-emerald-300",
    standard: "bg-sky-500/20 text-sky-300",
    substandard: "bg-amber-500/20 text-amber-300",
    decline: "bg-red-500/20 text-red-300",
  };

  return (
    <button
      onClick={onClick}
      className={`
        w-full text-left px-3 py-3 rounded-lg border transition-all group
        ${isSelected
          ? "bg-violet-500/15 border-violet-500/40 shadow-sm shadow-violet-500/10"
          : "bg-slate-800/40 border-slate-700/30 hover:bg-slate-800/70 hover:border-slate-600/50"
        }
      `}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className={`text-xs font-semibold truncate ${isSelected ? "text-violet-200" : "text-slate-200"}`}>
            {app.application_number}
          </p>
          <p className="text-[10px] text-slate-500 mt-0.5 capitalize">{app.policy_type}</p>
        </div>
        {app.risk_tier && (
          <span className={`flex-shrink-0 text-[9px] font-bold px-1.5 py-0.5 rounded-full ${tierColors[app.risk_tier] ?? "bg-slate-700 text-slate-400"}`}>
            {app.risk_tier.toUpperCase()}
          </span>
        )}
      </div>
      {app.underwriting_score !== null && (
        <div className="mt-2 flex items-center gap-2">
          <div className="flex-1 h-1 rounded-full bg-slate-700 overflow-hidden">
            <div
              className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-red-500 transition-all"
              style={{ width: `${Math.min(100, app.underwriting_score)}%` }}
            />
          </div>
          <span className="text-[10px] text-slate-400 tabular-nums">{app.underwriting_score}pts</span>
        </div>
      )}
    </button>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function UnderwritingDashboardPage() {
  const [applications, setApplications] = useState<Application[]>([]);
  const [selectedApp, setSelectedApp] = useState<Application | null>(null);
  const [decision, setDecision] = useState<UnderwritingDecision | null>(null);
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const [isLoadingApps, setIsLoadingApps] = useState(true);
  const [isLoadingDecision, setIsLoadingDecision] = useState(false);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);

  // ── Load application queue ────────────────────────────────────────────────
  useEffect(() => {
    setIsLoadingApps(true);
    applicationsAPI
      .list()
      .then((res) => setApplications(res.items))
      .catch(console.error)
      .finally(() => setIsLoadingApps(false));
  }, []);

  // ── Load decision when app is selected ───────────────────────────────────
  useEffect(() => {
    if (!selectedApp) {
      setDecision(null);
      return;
    }
    setIsLoadingDecision(true);
    setActiveCitation(null);
    applicationsAPI
      .getDecision(selectedApp.id)
      .then(setDecision)
      .catch(() => setDecision(null))
      .finally(() => setIsLoadingDecision(false));
  }, [selectedApp]);

  const handleCitationClick = useCallback((citation: Citation) => {
    setActiveCitation(citation);
  }, []);

  const handleAction = useCallback(
    (action: "approve" | "decline" | "postpone") => {
      if (!selectedApp) return;
      const labels = { approve: "Approved ✓", decline: "Declined ✗", postpone: "Postponed ⏸" };
      setActionFeedback(`${labels[action]} — ${selectedApp.application_number}`);
      setTimeout(() => setActionFeedback(null), 4000);
    },
    [selectedApp]
  );

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      {/* ── Top Nav ─────────────────────────────────────────────────────── */}
      <header className="flex-shrink-0 h-14 border-b border-slate-800 bg-slate-900/80 backdrop-blur-md flex items-center justify-between px-6 sticky top-0 z-50">
        <div className="flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-xs font-bold">
            AI
          </div>
          <span className="text-sm font-semibold text-slate-100">
            Healthcare Intelligence Platform
          </span>
          <span className="hidden sm:block text-slate-600">·</span>
          <span className="hidden sm:block text-xs text-slate-400">
            Underwriting Review
          </span>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-emerald-400" />
          <span className="text-xs text-slate-400">Systems Operational</span>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* ── Application Queue Sidebar ──────────────────────────────────── */}
        <aside className="w-64 flex-shrink-0 border-r border-slate-800 bg-slate-900/40 flex flex-col">
          <div className="px-4 pt-4 pb-3 border-b border-slate-800/60">
            <h2 className="text-xs font-semibold text-slate-300 uppercase tracking-widest">
              Review Queue
            </h2>
            <p className="text-[10px] text-slate-500 mt-0.5">
              {applications.length} applications
            </p>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {isLoadingApps ? (
              Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="h-16 rounded-lg bg-slate-800/60 animate-pulse" />
              ))
            ) : applications.length === 0 ? (
              <p className="text-xs text-slate-500 text-center py-6">No applications in queue.</p>
            ) : (
              applications.map((app) => (
                <ApplicationRow
                  key={app.id}
                  app={app}
                  isSelected={selectedApp?.id === app.id}
                  onClick={() => setSelectedApp(app)}
                />
              ))
            )}
          </div>
        </aside>

        {/* ── Main Workspace ─────────────────────────────────────────────── */}
        <main className="flex-1 flex flex-col min-w-0 p-4 gap-4">
          {/* Application Header */}
          {selectedApp && (
            <div className="flex-shrink-0 flex items-center justify-between">
              <div>
                <h1 className="text-base font-semibold text-slate-100">
                  {selectedApp.application_number}
                </h1>
                <p className="text-xs text-slate-500">
                  {selectedApp.policy_type} · submitted {new Date(selectedApp.created_at).toLocaleDateString()}
                </p>
              </div>
              {actionFeedback && (
                <div className="px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-xs text-slate-300 animate-in fade-in duration-200">
                  {actionFeedback}
                </div>
              )}
            </div>
          )}

          {/* HITL Workspace: GenAI + PDF side by side */}
          <div className="flex-1 grid grid-cols-1 lg:grid-cols-2 gap-4 min-h-0">
            <GenAIAssistant
              applicationId={selectedApp?.id ?? null}
              onCitationClick={handleCitationClick}
              className="min-h-0"
            />
            <CitationViewer
              activeCitation={activeCitation}
              className="min-h-0"
            />
          </div>

          {/* Score Card + Decision Panel */}
          {selectedApp && (
            <div className="flex-shrink-0 space-y-3">
              {isLoadingDecision ? (
                <div className="h-24 rounded-xl bg-slate-800/60 animate-pulse" />
              ) : (
                <ScoreCard decision={decision} />
              )}

              {/* Action Bar — only shown for manual review queue cases */}
              {decision?.route === "manual_review" && (
                <div className="flex items-center gap-3 justify-end">
                  <p className="text-xs text-slate-500 mr-auto">
                    Human review required — AI cannot make this decision autonomously.
                  </p>
                  <button
                    onClick={() => handleAction("postpone")}
                    className="px-4 py-2 text-xs font-medium rounded-lg bg-slate-700/60 text-slate-300 hover:bg-slate-700 border border-slate-600/50 transition-colors"
                  >
                    Postpone
                  </button>
                  <button
                    onClick={() => handleAction("decline")}
                    className="px-4 py-2 text-xs font-medium rounded-lg bg-red-500/15 text-red-300 hover:bg-red-500/25 border border-red-500/30 transition-colors"
                  >
                    Decline
                  </button>
                  <button
                    onClick={() => handleAction("approve")}
                    className="px-4 py-2 text-xs font-medium rounded-lg bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 border border-emerald-500/30 transition-colors"
                  >
                    Approve & Issue
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Empty state */}
          {!selectedApp && (
            <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center">
              <div className="w-16 h-16 rounded-2xl bg-slate-800/80 border border-slate-700 flex items-center justify-center text-slate-500 text-2xl">
                📋
              </div>
              <div>
                <p className="text-sm font-medium text-slate-300">Select an application</p>
                <p className="text-xs text-slate-500 mt-1 max-w-xs">
                  Choose a case from the review queue to launch the AI underwriting analysis.
                </p>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
