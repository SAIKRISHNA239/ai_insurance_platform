"use client";

/**
 * components/ui/CitationViewer.tsx
 * ──────────────────────────────────────────────────────────────────────────────
 * Interactive PDF viewer with bounding box highlight rendering.
 *
 * IMPLEMENTATION ARCHITECTURE
 * ─────────────────────────────
 * We use the `react-pdf` library (built on pdf.js) rather than an `<iframe>`
 * for these critical reasons:
 *   1. Page-level programmatic navigation without browser URL hacks.
 *   2. Access to the canvas rendering context for drawing bounding boxes.
 *   3. No CSP/CORS issues with same-origin-restricted iframe src.
 *
 * BOUNDING BOX RENDERING PIPELINE
 * ─────────────────────────────────
 * When `activeCitation` changes:
 *   1. Jump the <Document> to `activeCitation.bounding_box.page`.
 *   2. On the `onRenderSuccess` callback (after pdf.js paints the canvas),
 *      draw the highlight overlay onto a sibling <canvas> element positioned
 *      absolutely over the PDF canvas.
 *   3. The bounding box values [0–1] are multiplied by the rendered canvas
 *      dimensions (in pixels) to compute absolute pixel coordinates.
 *
 * WHY AN OVERLAY CANVAS INSTEAD OF SVG?
 * pdf.js renders to a <canvas>. We cannot insert SVG inside canvas.
 * A sibling overlay canvas with pointer-events:none is the standard approach
 * used by PDF annotation tools (Adobe, Hypothesis.is, etc.).
 *
 * COORDINATE SYSTEM NOTE
 * ─────────────────────────
 * BoundingBox uses [0–1] fractional coordinates (origin top-left).
 * This makes the highlight resolution-independent — the same citation
 * renders correctly at any zoom level or device pixel ratio.
 */

import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { type Citation } from "@/lib/api";

// ─── react-pdf dynamic import ──────────────────────────────────────────────────
// We lazy-import to avoid SSR issues (pdf.js uses browser-only APIs).
let Document: React.ComponentType<Record<string, unknown>> | null = null;
let Page: React.ComponentType<Record<string, unknown>> | null = null;

if (typeof window !== "undefined") {
  // Dynamic import resolved on mount via useEffect
}

// ─── Types ─────────────────────────────────────────────────────────────────────

interface CitationViewerProps {
  /** The citation currently selected in the GenAIAssistant. */
  activeCitation: Citation | null;
  className?: string;
}

interface RenderedPageSize {
  width: number;
  height: number;
}

// ─── Highlight Overlay Renderer ────────────────────────────────────────────────

/**
 * Draws the bounding box highlight on the overlay canvas.
 * Called after pdf.js finishes rendering the PDF page.
 *
 * @param overlayCanvas  The overlay canvas element (sibling to pdf.js canvas).
 * @param box            BoundingBox with fractional [0–1] coordinates.
 * @param pageSize       Rendered page dimensions in CSS pixels.
 */
function drawBoundingBox(
  overlayCanvas: HTMLCanvasElement,
  box: { x: number; y: number; width: number; height: number },
  pageSize: RenderedPageSize
): void {
  const ctx = overlayCanvas.getContext("2d");
  if (!ctx) return;

  // Match overlay canvas size to PDF page canvas size
  overlayCanvas.width = pageSize.width;
  overlayCanvas.height = pageSize.height;

  // Clear previous highlight
  ctx.clearRect(0, 0, pageSize.width, pageSize.height);

  const px = box.x * pageSize.width;
  const py = box.y * pageSize.height;
  const pw = box.width * pageSize.width;
  const ph = box.height * pageSize.height;

  // Highlight fill: semi-transparent amber (standard document annotation color)
  ctx.fillStyle = "rgba(251, 191, 36, 0.28)";
  ctx.fillRect(px, py, pw, ph);

  // Highlight border: solid amber
  ctx.strokeStyle = "rgba(245, 158, 11, 0.85)";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(px, py, pw, ph);

  // Subtle glow effect for premium UX
  ctx.shadowColor = "rgba(245, 158, 11, 0.4)";
  ctx.shadowBlur = 8;
  ctx.strokeRect(px, py, pw, ph);
}

// ─── Main Component ────────────────────────────────────────────────────────────

export default function CitationViewer({
  activeCitation,
  className = "",
}: CitationViewerProps) {
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);
  const [zoom, setZoom] = useState(1.0);
  const [pageSize, setPageSize] = useState<RenderedPageSize | null>(null);
  const [pdfModule, setPdfModule] = useState<{
    Document: React.ComponentType<Record<string, unknown>>;
    Page: React.ComponentType<Record<string, unknown>>;
  } | null>(null);

  const overlayCanvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // ── Lazy-load react-pdf on client ──────────────────────────────────────────
  useEffect(() => {
    import("react-pdf").then((mod) => {
      // Configure pdf.js worker
      const { pdfjs } = mod;
      pdfjs.GlobalWorkerOptions.workerSrc = `//unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`;

      setPdfModule({
        Document: mod.Document as React.ComponentType<Record<string, unknown>>,
        Page: mod.Page as React.ComponentType<Record<string, unknown>>,
      });
    });
  }, []);

  // ── Jump to cited page when activeCitation changes ─────────────────────────
  useEffect(() => {
    if (activeCitation) {
      setCurrentPage(activeCitation.bounding_box.page);
    }
  }, [activeCitation]);

  // ── Draw bounding box after page renders ───────────────────────────────────
  useLayoutEffect(() => {
    if (!activeCitation || !pageSize || !overlayCanvasRef.current) return;
    if (activeCitation.bounding_box.page !== currentPage) return;

    drawBoundingBox(
      overlayCanvasRef.current,
      activeCitation.bounding_box,
      pageSize
    );
  }, [activeCitation, currentPage, pageSize]);

  const onDocumentLoadSuccess = useCallback(
    ({ numPages }: { numPages: number }) => setTotalPages(numPages),
    []
  );

  const onPageRenderSuccess = useCallback(
    (page: { width: number; height: number }) => {
      const size = { width: page.width, height: page.height };
      setPageSize(size);
    },
    []
  );

  const clearOverlay = useCallback(() => {
    if (overlayCanvasRef.current) {
      const ctx = overlayCanvasRef.current.getContext("2d");
      ctx?.clearRect(
        0,
        0,
        overlayCanvasRef.current.width,
        overlayCanvasRef.current.height
      );
    }
    setPageSize(null);
  }, []);

  const documentUrl = activeCitation?.document_url ?? null;

  return (
    <div
      className={`flex flex-col h-full bg-slate-900/60 border border-slate-700/50 rounded-xl overflow-hidden backdrop-blur-sm ${className}`}
    >
      {/* Toolbar */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-700/50 bg-slate-800/50 flex-shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-100">
            {activeCitation?.document_name ?? "Document Viewer"}
          </span>
          {activeCitation && (
            <span className="px-1.5 py-0.5 text-[10px] rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">
              p. {activeCitation.bounding_box.page}
            </span>
          )}
        </div>

        {/* Pagination + Zoom */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            <button
              onClick={() => setZoom((z) => Math.max(0.5, z - 0.1))}
              className="w-6 h-6 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition-colors text-sm flex items-center justify-center"
              aria-label="Zoom out"
            >
              −
            </button>
            <span className="text-xs text-slate-400 w-10 text-center">
              {Math.round(zoom * 100)}%
            </span>
            <button
              onClick={() => setZoom((z) => Math.min(2.5, z + 0.1))}
              className="w-6 h-6 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition-colors text-sm flex items-center justify-center"
              aria-label="Zoom in"
            >
              +
            </button>
          </div>

          {totalPages > 0 && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => {
                  clearOverlay();
                  setCurrentPage((p) => Math.max(1, p - 1));
                }}
                disabled={currentPage <= 1}
                className="w-6 h-6 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-700 disabled:opacity-30 transition-colors flex items-center justify-center"
                aria-label="Previous page"
              >
                ‹
              </button>
              <span className="text-xs text-slate-400 tabular-nums">
                {currentPage} / {totalPages}
              </span>
              <button
                onClick={() => {
                  clearOverlay();
                  setCurrentPage((p) => Math.min(totalPages, p + 1));
                }}
                disabled={currentPage >= totalPages}
                className="w-6 h-6 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-700 disabled:opacity-30 transition-colors flex items-center justify-center"
                aria-label="Next page"
              >
                ›
              </button>
            </div>
          )}
        </div>
      </div>

      {/* PDF Canvas Area */}
      <div
        ref={containerRef}
        className="flex-1 overflow-auto flex justify-center items-start p-4 bg-slate-950/50"
      >
        {!documentUrl && (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-center py-12">
            <div className="w-12 h-12 rounded-xl bg-slate-800 border border-slate-700 flex items-center justify-center text-slate-500 text-xl">
              📄
            </div>
            <p className="text-sm text-slate-500">
              Click a citation in the AI summary to view the source document.
            </p>
          </div>
        )}

        {documentUrl && pdfModule && (
          <div className="relative shadow-2xl shadow-black/50">
            {/* @ts-ignore — react-pdf types require complex generics */}
            <pdfModule.Document
              file={documentUrl}
              onLoadSuccess={onDocumentLoadSuccess}
              loading={
                <div className="w-[600px] h-[800px] bg-slate-800 animate-pulse rounded" />
              }
              error={
                <div className="w-[600px] h-[400px] flex items-center justify-center bg-slate-800 rounded">
                  <p className="text-sm text-red-400">Failed to load PDF.</p>
                </div>
              }
            >
              {/* @ts-ignore */}
              <pdfModule.Page
                pageNumber={currentPage}
                scale={zoom}
                onRenderSuccess={onPageRenderSuccess}
                renderTextLayer={false}
                renderAnnotationLayer={false}
              />
            </pdfModule.Document>

            {/* Bounding box overlay canvas — positioned absolutely over PDF */}
            <canvas
              ref={overlayCanvasRef}
              className="absolute top-0 left-0 pointer-events-none"
              style={{
                width: pageSize?.width ?? 0,
                height: pageSize?.height ?? 0,
              }}
              aria-hidden="true"
            />

            {/* Citation excerpt tooltip when active */}
            {activeCitation && activeCitation.bounding_box.page === currentPage && (
              <div className="
                absolute bottom-4 left-1/2 -translate-x-1/2
                px-3 py-2 rounded-lg
                bg-slate-800/95 border border-amber-500/40
                text-xs text-slate-200 max-w-sm text-center
                shadow-xl backdrop-blur-sm
                animate-in fade-in slide-in-from-bottom-2 duration-200
              ">
                <p className="text-amber-300 font-semibold text-[10px] mb-0.5">
                  CITED PASSAGE
                </p>
                "{activeCitation.excerpt.slice(0, 120)}
                {activeCitation.excerpt.length > 120 ? "…" : ""}"
              </div>
            )}
          </div>
        )}

        {/* Loading state while pdfModule lazily imports */}
        {documentUrl && !pdfModule && (
          <div className="w-[600px] h-[800px] bg-slate-800 animate-pulse rounded" />
        )}
      </div>
    </div>
  );
}
