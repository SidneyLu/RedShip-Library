import { useEffect, useMemo, useRef, useState } from "react";
import { isDegenerateBbox } from "@/lib/ocrBbox";
import { ocrBlockColor } from "@/lib/ocrBlockColors";
import { cn } from "@/lib/utils";

export type LayoutOverlayBlock = {
  bbox: number[];
  type?: string;
  id?: string;
};

type Props = {
  pdfUrl?: string;
  page?: number;
  pageCount?: number;
  onPageChange?: (page: number) => void;
  layoutBlocks?: LayoutOverlayBlock[];
  activeBlockIndex?: number | null;
  onBlockClick?: (index: number) => void;
  hidePager?: boolean;
  className?: string;
};

export function PdfReader({
  pdfUrl,
  page = 1,
  pageCount,
  onPageChange,
  layoutBlocks,
  activeBlockIndex = null,
  onBlockClick,
  hidePager = false,
  className,
}: Props) {
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [currentPage, setCurrentPage] = useState(Math.max(1, page));
  const [docPages, setDocPages] = useState(0);
  const [rendering, setRendering] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pdfRef = useRef<import("pdfjs-dist").PDFDocumentProxy | null>(null);
  const renderTaskRef = useRef<{ cancel: () => void } | null>(null);

  useEffect(() => setReady(true), []);

  useEffect(() => {
    setCurrentPage(Math.max(1, page));
  }, [page]);

  const effectiveCount = pageCount && pageCount > 0 ? pageCount : docPages || 0;

  const goTo = (next: number) => {
    const capped =
      effectiveCount > 0 ? Math.min(Math.max(1, next), effectiveCount) : Math.max(1, next);
    setCurrentPage(capped);
    onPageChange?.(capped);
  };

  useEffect(() => {
    if (!ready) return;
    if (!pdfUrl) {
      setError("缺少 pdfUrl");
      setSourceUrl(null);
      return;
    }
    setError(null);
    setSourceUrl(pdfUrl);
  }, [pdfUrl, ready]);

  useEffect(() => {
    if (!ready || !sourceUrl) return;
    let cancelled = false;

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url
        ).toString();
        if (pdfRef.current) {
          await pdfRef.current.destroy().catch(() => undefined);
          pdfRef.current = null;
        }
        setDocPages(0);
        const pdf = await pdfjs.getDocument(sourceUrl).promise;
        if (cancelled) {
          await pdf.destroy().catch(() => undefined);
          return;
        }
        pdfRef.current = pdf;
        setDocPages(pdf.numPages);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(String((e as Error)?.message || e));
      }
    })();

    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
      pdfRef.current?.destroy().catch(() => undefined);
      pdfRef.current = null;
    };
  }, [sourceUrl, ready]);

  useEffect(() => {
    if (!ready || !pdfRef.current || !canvasRef.current) return;
    let cancelled = false;
    const pdf = pdfRef.current;
    const pageNum = Math.min(Math.max(1, currentPage), pdf.numPages || currentPage);

    (async () => {
      setRendering(true);
      try {
        renderTaskRef.current?.cancel();
        const pdfPage = await pdf.getPage(pageNum);
        if (cancelled) return;
        const container = containerRef.current;
        const base = pdfPage.getViewport({ scale: 1 });
        const maxWidth = Math.max(280, (container?.clientWidth || 720) - 24);
        const scale = Math.min(2.5, maxWidth / base.width);
        const viewport = pdfPage.getViewport({ scale });
        const canvas = canvasRef.current!;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        const task = pdfPage.render({ canvasContext: ctx, viewport });
        renderTaskRef.current = task;
        await task.promise;
      } catch (e) {
        const msg = String((e as Error)?.message || e);
        if (!cancelled && !msg.includes("Rendering cancelled")) setError(msg);
      } finally {
        if (!cancelled) setRendering(false);
      }
    })();

    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
    };
  }, [currentPage, docPages, sourceUrl, ready]);

  const overlayFromLayout = useMemo(() => {
    if (!layoutBlocks?.length) return [];
    return layoutBlocks
      .map((b, index) => ({ ...b, index }))
      .filter((b) => Array.isArray(b.bbox) && b.bbox.length >= 4 && !isDegenerateBbox(b.bbox));
  }, [layoutBlocks]);

  const atLast = effectiveCount > 0 ? currentPage >= effectiveCount : false;
  const showLoading = Boolean(!ready || !sourceUrl || (sourceUrl && docPages === 0 && !error));

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col", className)}>
      {!hidePager ? (
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2 text-sm">
          <button
            type="button"
            className="rounded px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-40"
            disabled={currentPage <= 1 || showLoading}
            onClick={() => goTo(currentPage - 1)}
          >
            上一页
          </button>
          <span className="text-muted">
            第 {currentPage} 页{effectiveCount > 0 ? ` / ${effectiveCount}` : ""}
            {rendering ? " · 渲染中" : ""}
          </span>
          <button
            type="button"
            className="rounded px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-40"
            disabled={atLast || showLoading}
            onClick={() => goTo(currentPage + 1)}
          >
            下一页
          </button>
        </div>
      ) : null}

      {error ? (
        <div className="flex flex-1 items-center justify-center p-6 text-sm text-crimson-700">
          无法加载 PDF：{error}
        </div>
      ) : showLoading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted">加载 PDF…</div>
      ) : (
        <div ref={containerRef} className="relative min-h-0 flex-1 overflow-auto bg-canvas">
          <div className="relative mx-auto w-fit p-3">
            <canvas ref={canvasRef} className="block max-w-full shadow-sm" />
            {overlayFromLayout.length > 0 ? (
              <div className="absolute inset-3">
                {overlayFromLayout.map((b) => {
                  const [x0, y0, x1, y1] = b.bbox;
                  const color = ocrBlockColor(b.type);
                  const active = activeBlockIndex === b.index;
                  return (
                    <button
                      key={b.id || b.index}
                      type="button"
                      onClick={() => onBlockClick?.(b.index)}
                      className={cn(
                        "absolute rounded-sm transition",
                        active ? "z-10 ring-2 ring-crimson-500 ring-offset-1" : "z-0"
                      )}
                      style={{
                        left: `${(x0 / 1000) * 100}%`,
                        top: `${(y0 / 1000) * 100}%`,
                        width: `${Math.max(((x1 - x0) / 1000) * 100, 0.5)}%`,
                        height: `${Math.max(((y1 - y0) / 1000) * 100, 0.5)}%`,
                        backgroundColor: color.fill,
                        border: `${active ? 2 : 1}px solid ${color.border}`,
                      }}
                    />
                  );
                })}
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
