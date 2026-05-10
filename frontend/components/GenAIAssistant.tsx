"use client";

/**
 * components/GenAIAssistant.tsx
 * ──────────────────────────────────────────────────────────────────────────────
 * Streaming GenAI chat interface with citation-first rendering.
 *
 * CITATION PARSING LOGIC
 * ───────────────────────
 * The FastAPI backend instructs the LLM to annotate its response with
 * citation markers using the format [^N] where N is an integer ID.
 * Example LLM output:
 *   "The applicant has a history of atrial fibrillation [^1] and was
 *    prescribed warfarin in Q2 2024 [^2]."
 *
 * This component:
 *   1. Receives tokens progressively from the SSE stream.
 *   2. Receives the final `Citation[]` array when the stream ends.
 *   3. On render, splits the accumulated text on the regex /\[\^(\d+)\]/g.
 *   4. Replaces each marker with a clickable <CitationBadge> that fires
 *      the `onCitationClick(citation)` callback.
 *   5. The parent dashboard wires `onCitationClick` to the CitationViewer,
 *      which jumps the PDF to the cited page and draws the bounding box.
 *
 * STREAMING STATE MACHINE
 * ─────────────────────────
 * idle → streaming → done | error
 * Abort is handled via the AbortController returned by `streamUnderwritingAssistant`.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { type Citation, streamUnderwritingAssistant } from "@/lib/api";

// ─── Types ─────────────────────────────────────────────────────────────────────

type StreamState = "idle" | "streaming" | "done" | "error";

interface GenAIAssistantProps {
  applicationId: string | null;
  onCitationClick: (citation: Citation) => void;
  className?: string;
}

// ─── Citation Badge ────────────────────────────────────────────────────────────

interface CitationBadgeProps {
  id: number;
  citation: Citation | undefined;
  onClick: (citation: Citation) => void;
}

function CitationBadge({ id, citation, onClick }: CitationBadgeProps) {
  if (!citation) {
    return (
      <sup className="inline-flex items-center justify-center w-4 h-4 text-[9px] font-bold rounded-full bg-slate-600 text-slate-300 mx-0.5 cursor-default">
        {id}
      </sup>
    );
  }
  return (
    <button
      onClick={() => onClick(citation)}
      title={`Source: ${citation.document_name} — "${citation.excerpt.slice(0, 80)}..."`}
      className="
        inline-flex items-center justify-center
        w-4 h-4 mx-0.5 rounded-full
        text-[9px] font-bold
        bg-violet-500/20 text-violet-300
        border border-violet-500/40
        hover:bg-violet-500/40 hover:border-violet-400
        transition-all duration-150 cursor-pointer
        focus:outline-none focus:ring-1 focus:ring-violet-400
      "
      aria-label={`Citation ${id}: ${citation.document_name}`}
    >
      {id}
    </button>
  );
}

// ─── Text Renderer with Citation Markers ───────────────────────────────────────

/**
 * Parses the LLM response text and replaces [^N] markers with
 * interactive CitationBadge components.
 *
 * Regex: /\[\^(\d+)\]/g
 * Splits text into alternating [text, markerGroup, text, markerGroup, ...]
 * The captured group (digits) is the citation ID.
 */
function CitationText({
  text,
  citations,
  onCitationClick,
}: {
  text: string;
  citations: Citation[];
  onCitationClick: (c: Citation) => void;
}) {
  const citationMap = new Map(citations.map((c) => [c.id, c]));
  const CITATION_RE = /\[\^(\d+)\]/g;

  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = CITATION_RE.exec(text)) !== null) {
    // Text before the marker
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const citId = parseInt(match[1], 10);
    parts.push(
      <CitationBadge
        key={`cite-${citId}-${match.index}`}
        id={citId}
        citation={citationMap.get(citId)}
        onClick={onCitationClick}
      />
    );
    lastIndex = match.index + match[0].length;
  }

  // Remaining text after the last marker
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return (
    <span className="leading-relaxed whitespace-pre-wrap font-mono text-sm text-slate-200">
      {parts}
    </span>
  );
}

// ─── Main Component ────────────────────────────────────────────────────────────

export default function GenAIAssistant({
  applicationId,
  onCitationClick,
  className = "",
}: GenAIAssistantProps) {
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [accumulatedText, setAccumulatedText] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom as new tokens arrive
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [accumulatedText]);

  const startStream = useCallback(() => {
    if (!applicationId || streamState === "streaming") return;

    // Reset state
    setAccumulatedText("");
    setCitations([]);
    setErrorMsg(null);
    setStreamState("streaming");

    abortRef.current = streamUnderwritingAssistant(applicationId, {
      onToken: (token) =>
        setAccumulatedText((prev) => prev + token),

      onCitations: (newCitations) =>
        setCitations(newCitations),

      onError: (err) => {
        setErrorMsg(err);
        setStreamState("error");
      },

      onDone: () => setStreamState("done"),
    });
  }, [applicationId, streamState]);

  const cancelStream = useCallback(() => {
    abortRef.current?.abort();
    setStreamState("idle");
  }, []);

  // Auto-start when applicationId changes
  useEffect(() => {
    if (applicationId) {
      setStreamState("idle");
      setAccumulatedText("");
      setCitations([]);
    }
  }, [applicationId]);

  // Cleanup on unmount
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const isStreaming = streamState === "streaming";

  return (
    <div
      className={`flex flex-col h-full bg-slate-900/60 border border-slate-700/50 rounded-xl overflow-hidden backdrop-blur-sm ${className}`}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-700/50 bg-slate-800/50">
        <div className="flex items-center gap-2.5">
          <div className="w-2 h-2 rounded-full bg-violet-400 animate-pulse" />
          <h2 className="text-sm font-semibold text-slate-100 tracking-wide">
            AI Underwriting Assistant
          </h2>
          {citations.length > 0 && (
            <span className="px-1.5 py-0.5 text-[10px] rounded-full bg-violet-500/20 text-violet-300 border border-violet-500/30">
              {citations.length} sources
            </span>
          )}
        </div>

        <div className="flex gap-2">
          {isStreaming ? (
            <button
              onClick={cancelStream}
              className="px-3 py-1 text-xs rounded-md bg-red-500/20 text-red-300 border border-red-500/30 hover:bg-red-500/30 transition-colors"
            >
              Stop
            </button>
          ) : (
            <button
              onClick={startStream}
              disabled={!applicationId}
              className="
                px-3 py-1 text-xs rounded-md
                bg-violet-500/20 text-violet-300 border border-violet-500/40
                hover:bg-violet-500/30
                disabled:opacity-40 disabled:cursor-not-allowed
                transition-colors
              "
            >
              {streamState === "done" ? "Regenerate" : "Analyze"}
            </button>
          )}
        </div>
      </div>

      {/* Content area */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-5 py-4 space-y-4 scroll-smooth"
      >
        {streamState === "idle" && !accumulatedText && (
          <div className="flex flex-col items-center justify-center h-full text-center gap-3 py-12">
            <div className="w-10 h-10 rounded-full bg-violet-500/10 border border-violet-500/20 flex items-center justify-center text-violet-400 text-lg">
              ✦
            </div>
            <p className="text-sm text-slate-400 max-w-xs">
              {applicationId
                ? "Click Analyze to generate an AI underwriting summary with cited evidence."
                : "Select an application to begin."}
            </p>
          </div>
        )}

        {(accumulatedText || isStreaming) && (
          <div className="relative">
            <CitationText
              text={accumulatedText}
              citations={citations}
              onCitationClick={onCitationClick}
            />
            {/* Blinking cursor during streaming */}
            {isStreaming && (
              <span className="inline-block w-0.5 h-4 ml-0.5 bg-violet-400 animate-pulse align-text-bottom" />
            )}
          </div>
        )}

        {streamState === "error" && (
          <div className="rounded-lg bg-red-500/10 border border-red-500/30 px-4 py-3">
            <p className="text-xs text-red-300 font-mono">{errorMsg}</p>
          </div>
        )}
      </div>

      {/* Citation source list */}
      {citations.length > 0 && streamState === "done" && (
        <div className="border-t border-slate-700/50 px-5 py-3 bg-slate-800/30">
          <p className="text-[10px] text-slate-500 font-semibold uppercase tracking-widest mb-2">
            Sources
          </p>
          <div className="flex flex-col gap-1">
            {citations.map((c) => (
              <button
                key={c.id}
                onClick={() => onCitationClick(c)}
                className="
                  flex items-start gap-2 text-left px-3 py-1.5 rounded-md
                  bg-slate-800/50 hover:bg-violet-500/10
                  border border-transparent hover:border-violet-500/20
                  transition-all group
                "
              >
                <span className="flex-shrink-0 w-4 h-4 mt-0.5 rounded-full bg-violet-500/20 text-violet-300 flex items-center justify-center text-[9px] font-bold">
                  {c.id}
                </span>
                <div className="min-w-0">
                  <p className="text-xs text-slate-300 font-medium truncate group-hover:text-violet-300 transition-colors">
                    {c.document_name}
                  </p>
                  <p className="text-[10px] text-slate-500 truncate">
                    Page {c.bounding_box.page} — "{c.excerpt.slice(0, 60)}..."
                  </p>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
