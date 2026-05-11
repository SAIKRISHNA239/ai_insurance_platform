'use client';

/**
 * components/GenAIAssistant.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * Streaming GenAI chat interface with citation-first rendering.
 *
 * FALLBACK STRATEGY
 * ─────────────────
 * When the streaming endpoint returns an error (e.g. Gemini API key not set,
 * ChromaDB offline), the component falls back to displaying the
 * ai_underwriting_notes from the database. This keeps the UI functional even
 * without a live LLM connection.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { type Citation, type Application, streamUnderwritingAssistant } from '@/lib/api';

type StreamState = 'idle' | 'streaming' | 'done' | 'fallback' | 'error';

interface GenAIAssistantProps {
  applicationId: string | null;
  application?: Application | null;       // ← used for fallback AI notes
  onCitationClick: (citation: Citation) => void;
  className?: string;
}

// ── Citation Badge ─────────────────────────────────────────────────────────────

function CitationBadge({
  id,
  citation,
  onClick,
}: {
  id: number;
  citation: Citation | undefined;
  onClick: (c: Citation) => void;
}) {
  if (!citation) {
    return (
      <sup className="inline-flex items-center justify-center w-4 h-4 text-[9px] font-bold rounded-full bg-surface-variant text-on-surface-variant mx-0.5 cursor-default">
        {id}
      </sup>
    );
  }
  return (
    <button
      onClick={() => onClick(citation)}
      title={`Source: ${citation.document_name} — "${citation.excerpt.slice(0, 80)}..."`}
      className="
        inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5
        rounded bg-primary-container/20 border border-primary/40
        text-primary font-data-mono text-[11px]
        hover:bg-primary-container/40 transition-colors align-middle
        focus:ring-1 focus:ring-primary focus:outline-none
      "
      aria-label={`Citation ${id}: ${citation.document_name}`}
    >
      <span className="material-symbols-outlined text-[12px]">description</span>
      <span>{id}</span>
    </button>
  );
}

// ── Citation Text Renderer ─────────────────────────────────────────────────────

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
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
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
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));

  return (
    <span className="leading-relaxed whitespace-pre-wrap font-body-sm text-body-sm text-on-surface">
      {parts}
    </span>
  );
}

// ── Typing Animation ───────────────────────────────────────────────────────────

/** Animates text appearing character by character from a static string. */
function TypewriterText({
  text,
  citations,
  onCitationClick,
  onDone,
}: {
  text: string;
  citations: Citation[];
  onCitationClick: (c: Citation) => void;
  onDone: () => void;
}) {
  const [displayed, setDisplayed] = useState('');
  const indexRef = useRef(0);

  useEffect(() => {
    indexRef.current = 0;
    setDisplayed('');
    const id = setInterval(() => {
      indexRef.current += 3; // 3 chars per tick feels natural
      setDisplayed(text.slice(0, indexRef.current));
      if (indexRef.current >= text.length) {
        clearInterval(id);
        onDone();
      }
    }, 16);
    return () => clearInterval(id);
  }, [text, onDone]);

  return (
    <CitationText
      text={displayed}
      citations={citations}
      onCitationClick={onCitationClick}
    />
  );
}

// ── Main Component ─────────────────────────────────────────────────────────────

export default function GenAIAssistant({
  applicationId,
  application,
  onCitationClick,
  className = '',
}: GenAIAssistantProps) {
  const [streamState, setStreamState]         = useState<StreamState>('idle');
  const [accumulatedText, setAccumulatedText]  = useState('');
  const [fallbackText, setFallbackText]        = useState('');
  const [typewriterDone, setTypewriterDone]    = useState(false);
  const [citations, setCitations]              = useState<Citation[]>([]);
  const [errorMsg, setErrorMsg]                = useState<string | null>(null);

  const abortRef  = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new tokens
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [accumulatedText, fallbackText]);

  // Reset when application changes
  useEffect(() => {
    if (applicationId) {
      setStreamState('idle');
      setAccumulatedText('');
      setFallbackText('');
      setCitations([]);
      setErrorMsg(null);
      setTypewriterDone(false);
    }
  }, [applicationId]);

  // Cleanup on unmount
  useEffect(() => () => abortRef.current?.abort(), []);

  const startStream = useCallback(() => {
    if (!applicationId || streamState === 'streaming') return;
    setAccumulatedText('');
    setFallbackText('');
    setCitations([]);
    setErrorMsg(null);
    setStreamState('streaming');
    setTypewriterDone(false);

    abortRef.current = streamUnderwritingAssistant(applicationId, {
      onToken:     (token) => setAccumulatedText((prev) => prev + token),
      onCitations: (newCitations) => setCitations(newCitations),
      onError: (err) => {
        // ── Graceful fallback to DB notes ─────────────────────────────────
        const dbNotes = application?.ai_underwriting_notes;
        if (dbNotes) {
          setFallbackText(dbNotes);
          setStreamState('fallback');
        } else {
          setErrorMsg(`AI stream unavailable: ${err}. No database notes available.`);
          setStreamState('error');
        }
      },
      onDone: () => setStreamState('done'),
    });
  }, [applicationId, streamState, application]);

  const cancelStream = useCallback(() => {
    abortRef.current?.abort();
    setStreamState('idle');
  }, []);

  const isStreaming = streamState === 'streaming';
  const isDone      = streamState === 'done' || streamState === 'fallback';
  const showContent = accumulatedText || fallbackText;

  return (
    <div className={`flex flex-col bg-surface overflow-hidden ${className}`}>
      {/* ── Panel header ────────────────────────────────────────────────── */}
      <div className="h-14 px-6 flex items-center justify-between border-b border-outline-variant bg-surface-container-lowest shrink-0">
        <div className="flex items-center gap-2">
          <span
            className="material-symbols-outlined text-primary text-xl"
            style={{ fontVariationSettings: "'FILL' 1" }}
          >
            auto_awesome
          </span>
          <h2 className="font-headline-md text-headline-md text-on-surface">Medical Summary</h2>
          {citations.length > 0 && (
            <span className="px-1.5 py-0.5 font-label-caps text-label-caps rounded-full bg-primary-container/20 text-primary border border-primary/30">
              {citations.length} sources
            </span>
          )}
          {streamState === 'fallback' && (
            <span className="px-1.5 py-0.5 font-label-caps text-label-caps rounded-full bg-tertiary-container/20 text-tertiary border border-tertiary/30">
              DB Notes
            </span>
          )}
        </div>

        <div className="flex gap-2">
          {isStreaming ? (
            <button
              onClick={cancelStream}
              className="px-3 py-1 text-xs rounded-lg bg-error-container/20 text-error border border-error-container/30 hover:bg-error-container/30 transition-colors"
            >
              Stop
            </button>
          ) : (
            <button
              onClick={startStream}
              disabled={!applicationId}
              className="
                px-3 py-1 text-xs rounded-lg
                bg-primary-container/20 text-primary border border-primary/40
                hover:bg-primary-container/40
                disabled:opacity-40 disabled:cursor-not-allowed
                transition-colors
              "
            >
              {isDone ? 'Regenerate' : 'Analyze'}
            </button>
          )}
        </div>
      </div>

      {/* ── Content area ─────────────────────────────────────────────────── */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-6 space-y-6 scroll-smooth">

        {/* Idle empty state */}
        {streamState === 'idle' && !showContent && (
          <div className="flex flex-col items-center justify-center h-full text-center gap-3 py-12">
            <div className="w-10 h-10 rounded-full bg-primary/10 border border-primary/20 flex items-center justify-center">
              <span className="material-symbols-outlined text-primary">auto_awesome</span>
            </div>
            <p className="font-body-sm text-body-sm text-on-surface-variant max-w-xs">
              {applicationId
                ? 'Click Analyze to generate an AI underwriting summary with cited evidence.'
                : 'Select an application to begin.'}
            </p>
          </div>
        )}

        {/* Streaming / done text */}
        {(accumulatedText || isStreaming) && (
          <div className="rounded-lg border border-primary/30 bg-primary/5 p-5 space-y-4 relative overflow-hidden">
            <div className="absolute top-0 left-0 w-1 h-full bg-gradient-to-b from-primary to-secondary" />
            <div className="flex items-center justify-between">
              <span className="font-label-caps text-label-caps text-primary tracking-wider uppercase">
                AI Generated Synthesis
              </span>
              {isStreaming && (
                <span className="font-data-mono text-data-mono text-on-surface-variant text-xs animate-pulse">
                  Generating…
                </span>
              )}
            </div>
            <div className="font-body-sm text-body-sm text-on-surface leading-relaxed">
              <CitationText
                text={accumulatedText}
                citations={citations}
                onCitationClick={onCitationClick}
              />
              {isStreaming && (
                <span className="inline-block w-0.5 h-4 ml-0.5 bg-primary animate-pulse align-text-bottom" />
              )}
            </div>
          </div>
        )}

        {/* Fallback DB notes with typewriter animation */}
        {fallbackText && (
          <div className="rounded-lg border border-tertiary/30 bg-tertiary/5 p-5 space-y-4 relative overflow-hidden">
            <div className="absolute top-0 left-0 w-1 h-full bg-gradient-to-b from-tertiary to-primary" />
            <div className="flex items-center justify-between">
              <span className="font-label-caps text-label-caps text-tertiary tracking-wider uppercase">
                Underwriting Assessment
              </span>
              {!typewriterDone && (
                <span className="font-data-mono text-data-mono text-on-surface-variant text-xs animate-pulse">
                  Loading…
                </span>
              )}
            </div>
            <div className="font-body-sm text-body-sm text-on-surface leading-relaxed">
              <TypewriterText
                text={fallbackText}
                citations={[]}
                onCitationClick={onCitationClick}
                onDone={() => setTypewriterDone(true)}
              />
              {!typewriterDone && (
                <span className="inline-block w-0.5 h-4 ml-0.5 bg-tertiary animate-pulse align-text-bottom" />
              )}
            </div>
            <p className="font-label-caps text-label-caps text-on-surface-variant">
              ⚡ Loaded from database · Connect Gemini API for live analysis
            </p>
          </div>
        )}

        {/* Error state */}
        {streamState === 'error' && (
          <div className="rounded-lg bg-error-container/20 border border-error-container/30 px-4 py-3 flex items-start gap-3">
            <span className="material-symbols-outlined text-error text-[20px] mt-0.5 shrink-0">error</span>
            <div>
              <p className="font-body-sm text-body-sm text-error font-semibold">AI Analysis Unavailable</p>
              <p className="font-data-mono text-data-mono text-on-surface-variant mt-1 text-xs">{errorMsg}</p>
            </div>
          </div>
        )}
      </div>

      {/* ── Source list (shown after done) ──────────────────────────────── */}
      {citations.length > 0 && streamState === 'done' && (
        <div className="border-t border-outline-variant px-5 py-3 bg-surface-container-lowest shrink-0">
          <p className="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-widest mb-2">
            Sources
          </p>
          <div className="flex flex-col gap-1">
            {citations.map((c) => (
              <button
                key={c.id}
                onClick={() => onCitationClick(c)}
                className="
                  flex items-start gap-2 text-left px-3 py-1.5 rounded-lg
                  bg-surface-container hover:bg-primary/10
                  border border-transparent hover:border-primary/20
                  transition-all group
                "
              >
                <span className="flex-shrink-0 w-5 h-5 mt-0.5 rounded-full bg-primary-container/20 text-primary flex items-center justify-center font-label-caps text-label-caps">
                  {c.id}
                </span>
                <div className="min-w-0">
                  <p className="font-body-sm text-body-sm text-on-surface font-medium truncate group-hover:text-primary transition-colors">
                    {c.document_name}
                  </p>
                  <p className="font-data-mono text-data-mono text-on-surface-variant truncate">
                    Page {c.bounding_box.page} — &quot;{c.excerpt.slice(0, 60)}...&quot;
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
