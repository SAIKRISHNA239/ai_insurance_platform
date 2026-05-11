'use client';

/**
 * components/ui/CitationViewer.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * Interactive PDF viewer with bounding box highlight rendering.
 * Restyled to MD3 design tokens — all functional logic preserved.
 *
 * BOUNDING BOX RENDERING PIPELINE
 * ──────────────────────────────────
 * 1. activeCitation changes → jump to cited page.
 * 2. onRenderSuccess fires (pdf.js finishes canvas paint).
 * 3. drawBoundingBox() paints amber highlight on sibling overlay <canvas>.
 * 4. Fractional [0–1] coordinates × rendered page dimensions = pixel coords.
 */

import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { type Citation } from '@/lib/api';

// ── react-pdf lazy import (browser-only) ─────────────────────────────────────

interface RenderedPageSize { width: number; height: number; }

function drawBoundingBox(
  canvas: HTMLCanvasElement,
  box: { x: number; y: number; width: number; height: number },
  size: RenderedPageSize
): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  canvas.width  = size.width;
  canvas.height = size.height;
  ctx.clearRect(0, 0, size.width, size.height);

  const px = box.x * size.width;
  const py = box.y * size.height;
  const pw = box.width  * size.width;
  const ph = box.height * size.height;

  // Amber highlight — standard document annotation color
  ctx.fillStyle   = 'rgba(251, 191, 36, 0.25)';
  ctx.fillRect(px, py, pw, ph);
  ctx.strokeStyle = 'rgba(245, 158, 11, 0.85)';
  ctx.lineWidth   = 1.5;
  ctx.strokeRect(px, py, pw, ph);
  ctx.shadowColor = 'rgba(245, 158, 11, 0.4)';
  ctx.shadowBlur  = 8;
  ctx.strokeRect(px, py, pw, ph);
}

// ── Main Component ────────────────────────────────────────────────────────────

interface CitationViewerProps {
  activeCitation: Citation | null;
  className?: string;
}

export default function CitationViewer({ activeCitation, className = '' }: CitationViewerProps) {
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages,  setTotalPages]  = useState(0);
  const [zoom,        setZoom]        = useState(1.0);
  const [pageSize,    setPageSize]    = useState<RenderedPageSize | null>(null);
  const [pdfModule,   setPdfModule]   = useState<{
    Document: React.ComponentType<Record<string, unknown>>;
    Page:     React.ComponentType<Record<string, unknown>>;
  } | null>(null);

  const overlayCanvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef     = useRef<HTMLDivElement>(null);

  // Lazy-load react-pdf on client
  useEffect(() => {
    import('react-pdf').then((mod) => {
      const { pdfjs } = mod;
      pdfjs.GlobalWorkerOptions.workerSrc =
        `//unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`;
      setPdfModule({
        Document: mod.Document as React.ComponentType<Record<string, unknown>>,
        Page:     mod.Page     as React.ComponentType<Record<string, unknown>>,
      });
    });
  }, []);

  // Jump to cited page
  useEffect(() => {
    if (activeCitation) setCurrentPage(activeCitation.bounding_box.page);
  }, [activeCitation]);

  // Draw bounding box overlay after page renders
  useLayoutEffect(() => {
    if (!activeCitation || !pageSize || !overlayCanvasRef.current) return;
    if (activeCitation.bounding_box.page !== currentPage) return;
    drawBoundingBox(overlayCanvasRef.current, activeCitation.bounding_box, pageSize);
  }, [activeCitation, currentPage, pageSize]);

  const onDocumentLoadSuccess = useCallback(
    ({ numPages }: { numPages: number }) => setTotalPages(numPages), []
  );

  const onPageRenderSuccess = useCallback(
    (page: { width: number; height: number }) => setPageSize({ width: page.width, height: page.height }), []
  );

  const clearOverlay = useCallback(() => {
    if (overlayCanvasRef.current) {
      const ctx = overlayCanvasRef.current.getContext('2d');
      ctx?.clearRect(0, 0, overlayCanvasRef.current.width, overlayCanvasRef.current.height);
    }
    setPageSize(null);
  }, []);

  const documentUrl = activeCitation?.document_url ?? null;

  return (
    <div className={`flex flex-col bg-surface-container-lowest overflow-hidden ${className}`}>

      {/* ── Toolbar ─────────────────────────────────────────────────────── */}
      <div className="h-14 px-4 border-b border-outline-variant bg-surface-container flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <span className="material-symbols-outlined text-on-surface-variant">picture_as_pdf</span>
          <span className="font-body-sm font-semibold text-on-surface">
            {activeCitation?.document_name ?? 'Document Viewer'}
          </span>
          {activeCitation && (
            <span className="px-1.5 py-0.5 font-label-caps text-label-caps rounded-full bg-tertiary-container/20 text-tertiary border border-tertiary-container/30">
              p. {activeCitation.bounding_box.page}
            </span>
          )}
        </div>

        {/* Zoom + Pagination controls */}
        <div className="flex items-center gap-4 border-x border-outline-variant px-4 h-full">
          <button
            onClick={() => setZoom((z) => Math.max(0.5, z - 0.1))}
            className="w-8 h-8 flex items-center justify-center text-on-surface-variant hover:text-on-surface hover:bg-surface-variant rounded transition-colors"
            aria-label="Zoom out"
          >
            <span className="material-symbols-outlined text-[20px]">remove</span>
          </button>
          <span className="font-data-mono text-data-mono text-on-surface w-10 text-center">
            {Math.round(zoom * 100)}%
          </span>
          <button
            onClick={() => setZoom((z) => Math.min(2.5, z + 0.1))}
            className="w-8 h-8 flex items-center justify-center text-on-surface-variant hover:text-on-surface hover:bg-surface-variant rounded transition-colors"
            aria-label="Zoom in"
          >
            <span className="material-symbols-outlined text-[20px]">add</span>
          </button>
        </div>

        <div className="flex items-center gap-2">
          {totalPages > 0 && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => { clearOverlay(); setCurrentPage((p) => Math.max(1, p - 1)); }}
                disabled={currentPage <= 1}
                className="w-8 h-8 flex items-center justify-center text-on-surface-variant hover:text-on-surface hover:bg-surface-variant disabled:opacity-30 rounded transition-colors"
                aria-label="Previous page"
              >
                <span className="material-symbols-outlined text-[20px]">chevron_left</span>
              </button>
              <span className="font-data-mono text-data-mono text-on-surface-variant tabular-nums">
                {currentPage} / {totalPages}
              </span>
              <button
                onClick={() => { clearOverlay(); setCurrentPage((p) => Math.min(totalPages, p + 1)); }}
                disabled={currentPage >= totalPages}
                className="w-8 h-8 flex items-center justify-center text-on-surface-variant hover:text-on-surface hover:bg-surface-variant disabled:opacity-30 rounded transition-colors"
                aria-label="Next page"
              >
                <span className="material-symbols-outlined text-[20px]">chevron_right</span>
              </button>
            </div>
          )}
          <button className="w-8 h-8 flex items-center justify-center text-on-surface-variant hover:text-on-surface hover:bg-surface-variant rounded transition-colors">
            <span className="material-symbols-outlined text-[20px]">download</span>
          </button>
        </div>
      </div>

      {/* ── PDF Canvas Area ──────────────────────────────────────────────── */}
      <div ref={containerRef} className="flex-1 overflow-auto flex justify-center items-start p-8 bg-[#05070a]">
        {!documentUrl && (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-center py-12">
            <div className="w-12 h-12 rounded-xl bg-surface-container border border-outline-variant flex items-center justify-center text-on-surface-variant">
              <span className="material-symbols-outlined text-xl">picture_as_pdf</span>
            </div>
            <p className="font-body-sm text-body-sm text-on-surface-variant">
              Click a citation in the AI summary to view the source document.
            </p>
          </div>
        )}

        {documentUrl && pdfModule && (
          <div className="relative shadow-2xl shadow-black/50">
            {/* @ts-ignore — react-pdf complex generics */}
            <pdfModule.Document
              file={documentUrl}
              onLoadSuccess={onDocumentLoadSuccess}
              loading={<div className="w-[600px] h-[800px] bg-surface-container animate-pulse rounded" />}
              error={
                <div className="w-[600px] h-[400px] flex items-center justify-center bg-surface-container rounded">
                  <p className="font-body-sm text-body-sm text-error">Failed to load PDF.</p>
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

            {/* Bounding box overlay */}
            <canvas
              ref={overlayCanvasRef}
              className="absolute top-0 left-0 pointer-events-none"
              style={{ width: pageSize?.width ?? 0, height: pageSize?.height ?? 0 }}
              aria-hidden="true"
            />

            {/* Citation excerpt tooltip */}
            {activeCitation && activeCitation.bounding_box.page === currentPage && (
              <div className="
                absolute bottom-4 left-1/2 -translate-x-1/2
                px-3 py-2 rounded-lg
                bg-surface-container/95 border border-tertiary-container/40
                font-body-sm text-body-sm text-on-surface max-w-sm text-center
                shadow-xl backdrop-blur-sm
                animate-in fade-in slide-in-from-bottom-2 duration-200
              ">
                <p className="text-tertiary font-label-caps text-label-caps mb-0.5">CITED PASSAGE</p>
                &ldquo;{activeCitation.excerpt.slice(0, 120)}
                {activeCitation.excerpt.length > 120 ? '…' : ''}&rdquo;
              </div>
            )}
          </div>
        )}

        {documentUrl && !pdfModule && (
          <div className="w-[600px] h-[800px] bg-surface-container animate-pulse rounded" />
        )}
      </div>
    </div>
  );
}
