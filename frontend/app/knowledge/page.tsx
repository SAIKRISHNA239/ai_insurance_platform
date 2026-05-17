'use client';

import { useRef, useState } from 'react';
import {
  knowledgeAPI,
  mlopsAPI,
  type EvaluationStatus,
  type KnowledgeUploadResponse,
} from '@/lib/api';

export default function KnowledgePage() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging]   = useState(false);
  const [uploading,  setUploading]    = useState(false);
  const [result,     setResult]       = useState<KnowledgeUploadResponse | null>(null);
  const [error,      setError]        = useState<string | null>(null);

  // MLOps eval state
  const [evaluating,  setEvaluating]  = useState(false);
  const [evalResult,  setEvalResult]  = useState<EvaluationStatus | null>(null);
  const [evalError,   setEvalError]   = useState<string | null>(null);
  const [polling,     setPolling]     = useState(false);

  async function handleUpload(file: File) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setError('Only PDF files are accepted.');
      return;
    }
    setUploading(true);
    setResult(null);
    setError(null);
    try {
      const res = await knowledgeAPI.uploadDocument(file);
      setResult(res);
    } catch (err: unknown) {
      const e = err as { detail?: string };
      setError(String(e?.detail ?? err));
    } finally {
      setUploading(false);
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleUpload(file);
  }

  async function handleEvaluate() {
    setEvaluating(true);
    setEvalResult(null);
    setEvalError(null);
    try {
      const accepted = await mlopsAPI.triggerEvaluation();
      // Poll until complete or failed
      setPolling(true);
      let status: EvaluationStatus;
      do {
        await new Promise((r) => setTimeout(r, 2000));
        status = await mlopsAPI.getEvaluationStatus(accepted.run_id);
        setEvalResult(status);
      } while (status.state === 'queued' || status.state === 'running');
    } catch (err: unknown) {
      const e = err as { detail?: string };
      setEvalError(String(e?.detail ?? err));
    } finally {
      setEvaluating(false);
      setPolling(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#0a0f1e] text-white font-sans p-8">
      {/* Header */}
      <div className="mb-10">
        <h1 className="text-3xl font-bold bg-gradient-to-r from-indigo-400 to-violet-400 bg-clip-text text-transparent">
          Knowledge Base
        </h1>
        <p className="text-white/50 mt-1 text-sm">
          Upload policy PDFs to power the RAG underwriting assistant. Admin &amp; Underwriter only.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 max-w-5xl">

        {/* ── Upload card ─────────────────────────────────────── */}
        <section className="rounded-2xl bg-white/[0.04] border border-white/10 p-6 backdrop-blur-md shadow-xl">
          <div className="flex items-center gap-3 mb-6">
            <span className="material-symbols-outlined text-indigo-400 text-[22px]">upload_file</span>
            <h2 className="font-semibold text-base text-white/90">Upload Policy Document</h2>
          </div>

          {/* Drop zone */}
          <div
            id="knowledge-drop-zone"
            onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={onDrop}
            onClick={() => fileRef.current?.click()}
            className={`relative flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed cursor-pointer transition-all duration-200 py-12 px-6 text-center
              ${isDragging
                ? 'border-indigo-400 bg-indigo-500/10'
                : 'border-white/15 hover:border-indigo-400/50 hover:bg-white/[0.02]'}`}
          >
            <span className={`material-symbols-outlined text-[40px] transition-colors ${isDragging ? 'text-indigo-400' : 'text-white/30'}`}>
              description
            </span>
            <div>
              <p className="font-medium text-white/70 text-sm">
                {isDragging ? 'Drop to upload…' : 'Drag & drop a PDF here'}
              </p>
              <p className="text-white/30 text-xs mt-1">or click to browse · max 50 MB</p>
            </div>

            {uploading && (
              <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-black/40 backdrop-blur-sm">
                <span className="material-symbols-outlined text-indigo-400 text-[32px] animate-spin">sync</span>
              </div>
            )}
          </div>

          <input
            ref={fileRef}
            type="file"
            accept=".pdf"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleUpload(f);
              e.target.value = '';
            }}
          />

          {/* Error */}
          {error && (
            <div className="mt-4 rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3 flex items-center gap-2">
              <span className="material-symbols-outlined text-red-400 text-[16px]">error</span>
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {/* Success */}
          {result && (
            <div className="mt-4 rounded-xl bg-emerald-500/10 border border-emerald-500/20 px-4 py-4 space-y-2">
              <div className="flex items-center gap-2">
                <span className="material-symbols-outlined text-emerald-400 text-[18px]">check_circle</span>
                <span className="text-emerald-400 font-semibold text-sm">Ingestion queued</span>
              </div>
              <p className="text-white/60 text-xs">{result.message}</p>
              <div className="mt-2 bg-black/20 rounded-lg px-3 py-2 font-mono text-xs text-white/40 break-all">
                ID: {result.document_id}
              </div>
            </div>
          )}

          {/* Tips */}
          <div className="mt-5 space-y-2">
            {['Policy benefit schedules', 'Formulary PDFs', 'Clinical coverage guidelines', 'Underwriting manuals'].map((t) => (
              <div key={t} className="flex items-center gap-2 text-white/40 text-xs">
                <span className="material-symbols-outlined text-[13px] text-indigo-400/60">check</span>
                {t}
              </div>
            ))}
          </div>
        </section>

        {/* ── MLOps evaluation card ────────────────────────────── */}
        <section className="rounded-2xl bg-white/[0.04] border border-white/10 p-6 backdrop-blur-md shadow-xl flex flex-col">
          <div className="flex items-center gap-3 mb-4">
            <span className="material-symbols-outlined text-violet-400 text-[22px]">monitoring</span>
            <h2 className="font-semibold text-base text-white/90">RAG Quality Evaluation</h2>
          </div>
          <p className="text-white/40 text-sm mb-6">
            Run a RAGAS evaluation against 5 gold-standard clinical queries to measure faithfulness, precision, and recall.
          </p>

          <button
            id="run-evaluation-btn"
            onClick={handleEvaluate}
            disabled={evaluating}
            className="self-start inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-violet-600 hover:bg-violet-500 transition-colors text-sm font-semibold disabled:opacity-50 disabled:cursor-wait"
          >
            {evaluating ? (
              <><span className="material-symbols-outlined text-[16px] animate-spin">sync</span>{polling ? 'Polling…' : 'Starting…'}</>
            ) : (
              <><span className="material-symbols-outlined text-[16px]">play_arrow</span>Run Evaluation</>
            )}
          </button>

          {evalError && (
            <div className="mt-4 rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3">
              <p className="text-red-400 text-sm">{evalError}</p>
            </div>
          )}

          {evalResult && (
            <div className="mt-5 flex-1">
              <div className="flex items-center gap-2 mb-4">
                <span className={`w-2 h-2 rounded-full ${evalResult.state === 'complete' ? 'bg-emerald-400' : evalResult.state === 'failed' ? 'bg-red-400' : 'bg-amber-400 animate-pulse'}`} />
                <span className="text-white/60 text-xs uppercase tracking-widest font-semibold">{evalResult.state}</span>
              </div>

              {evalResult.scores && (
                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(evalResult.scores).map(([key, val]) => {
                    const pct = Math.round((val as number) * 100);
                    const color = pct >= 90 ? 'text-emerald-400' : pct >= 70 ? 'text-amber-400' : 'text-red-400';
                    return (
                      <div key={key} className="rounded-lg bg-black/20 border border-white/5 px-4 py-3">
                        <p className="text-white/40 text-[10px] uppercase tracking-widest mb-1">
                          {key.replace(/_/g, ' ')}
                        </p>
                        <p className={`text-2xl font-bold tabular-nums ${color}`}>{pct}%</p>
                      </div>
                    );
                  })}
                </div>
              )}

              {evalResult.error && (
                <p className="text-red-400 text-sm mt-3">{evalResult.error}</p>
              )}
            </div>
          )}
        </section>

      </div>
    </main>
  );
}
