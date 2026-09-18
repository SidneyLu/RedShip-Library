import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { OcrProgressBar, type OcrProgressState } from "@/components/OcrProgressBar";
import { ReviewPanel, type ReviewDoc } from "@/components/ReviewPanel";
import { DeliveryPanel } from "@/components/DeliveryPanel";
import { PdfReader } from "@/components/reader/PdfReader";
import {
  docArtifactUrl,
  docPdfUrl,
  getDocument,
  rerunPages,
  runOcr,
  runReview,
  stopOcr,
  subscribeJobEvents,
  type DocumentItem,
} from "@/lib/api";
import { ocrBlockColor } from "@/lib/ocrBlockColors";
import { pageHasEstimatedBboxes, repairPageBlocks } from "@/lib/ocrBbox";
import { cn } from "@/lib/utils";

type LayoutBlock = {
  type: string;
  text: string;
  bbox: number[];
  bboxEstimated?: boolean;
};

type LayoutDoc = {
  pages: Array<{ page: number; blocks: LayoutBlock[] }>;
};

type ResultTab = "blocks" | "markdown" | "review" | "delivery";

function extractPageMarkdown(md: string, page: number): string {
  const marker = `<!-- page: ${page} -->`;
  const start = md.indexOf(marker);
  if (start < 0) return md;
  const after = start + marker.length;
  const next = md.indexOf("<!-- page:", after);
  return (next < 0 ? md.slice(after) : md.slice(after, next)).trim();
}

function progressFromDoc(d: DocumentItem): OcrProgressState | null {
  if (d.status !== "ocr_running") return null;
  const job = d.ocr_job;
  const total = job?.total_pages || d.pages || 0;
  const current = job?.current_page ?? 0;
  return {
    current,
    total,
    label: total > 0 ? `识别中 ${current}/${total} 页` : "OCR 进行中…",
  };
}

export default function WorkbenchPage() {
  const { documentId } = useParams<{ documentId: string }>();
  const [doc, setDoc] = useState<DocumentItem | null>(null);
  const [layout, setLayout] = useState<LayoutDoc | null>(null);
  const [markdown, setMarkdown] = useState("");
  const [review, setReview] = useState<ReviewDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const [tab, setTab] = useState<ResultTab>("blocks");
  const [progress, setProgress] = useState<OcrProgressState | null>(null);
  const [ocrBusy, setOcrBusy] = useState(false);
  const blockRefs = useRef<Map<number, HTMLLIElement>>(new Map());
  const esRef = useRef<EventSource | null>(null);

  const loadArtifacts = useCallback(async () => {
    if (!documentId) return;
    setLoading(true);
    setError(null);
    try {
      const d = await getDocument(documentId);
      setDoc(d);
      if (d.status === "ocr_running") setProgress(progressFromDoc(d));
      const [layoutRes, mdRes, reviewRes] = await Promise.all([
        fetch(docArtifactUrl(documentId, "layout.json")),
        fetch(docArtifactUrl(documentId, "content.md")),
        fetch(docArtifactUrl(documentId, "review.json")),
      ]);
      if (layoutRes.ok) setLayout((await layoutRes.json()) as LayoutDoc);
      if (mdRes.ok) setMarkdown(await mdRes.text());
      if (reviewRes.ok) setReview((await reviewRes.json()) as ReviewDoc);
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, [documentId]);

  useEffect(() => {
    loadArtifacts();
  }, [loadArtifacts]);

  useEffect(() => {
    return () => {
      esRef.current?.close();
      esRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!documentId || doc?.status !== "ocr_running" || ocrBusy) return;
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        try {
          const d = await getDocument(documentId);
          if (cancelled) break;
          setDoc(d);
          setProgress(progressFromDoc(d));
          if (d.status !== "ocr_running") {
            await loadArtifacts();
            setProgress(null);
            break;
          }
        } catch {
          /* ignore */
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    };
    poll();
    return () => {
      cancelled = true;
    };
  }, [documentId, doc?.status, loadArtifacts, ocrBusy]);

  const pageCount = layout?.pages?.length ?? doc?.pages ?? 0;
  const currentBlocks = useMemo(() => {
    const p = layout?.pages?.find((x) => x.page === page);
    return repairPageBlocks(p?.blocks ?? []);
  }, [layout, page]);

  const estimated = pageHasEstimatedBboxes(currentBlocks);
  const pageMarkdown = useMemo(() => extractPageMarkdown(markdown, page), [markdown, page]);

  const goTo = (next: number) => {
    if (pageCount <= 0) return;
    const capped = Math.min(Math.max(1, Math.floor(next)), pageCount);
    setPage(capped);
    setPageInput(String(capped));
    setActiveIdx(null);
  };

  const commitPageInput = () => {
    const n = parseInt(pageInput.replace(/\D/g, ""), 10);
    if (!Number.isFinite(n) || n < 1) {
      setPageInput(String(page));
      return;
    }
    goTo(n);
  };

  useEffect(() => {
    setPageInput(String(page));
  }, [page]);

  const watchJob = (jobId: string, labelPrefix: string) => {
    esRef.current?.close();
    setOcrBusy(true);
    setProgress({ current: 0, total: 0, label: `${labelPrefix}已启动…` });
    const es = subscribeJobEvents(jobId, (ev) => {
      if (ev.type === "resume") {
        const skipped = Number(ev.skipped_pages) || 0;
        const remaining = Number(ev.remaining) || 0;
        const total = Number(ev.total_pages) || 0;
        setProgress({
          current: skipped,
          total: total || skipped + remaining,
          label: `续跑：已完成 ${skipped} 页，剩余 ${remaining} 页（共 ${total || "?"} 页）`,
        });
      }
      if (ev.type === "progress") {
        const current = Number(ev.current_page) || 0;
        const total = Number(ev.total_pages) || 0;
        setProgress({
          current,
          total,
          label: total > 0 ? `识别中 ${current}/${total} 页` : `识别中 ${current} 页`,
        });
      }
      if (ev.type === "done") {
        setProgress(null);
        setOcrBusy(false);
        loadArtifacts();
        es.close();
        esRef.current = null;
      }
      if (ev.type === "error") {
        setProgress({
          current: 0,
          total: 1,
          error: String(ev.error || "未知错误"),
          label: "OCR 失败（已识别页已保留，可继续 OCR）",
        });
        setOcrBusy(false);
        loadArtifacts();
        es.close();
        esRef.current = null;
      }
    });
    esRef.current = es;
  };

  const onRunOcr = async () => {
    if (!documentId || ocrBusy) return;
    try {
      const { job_id } = await runOcr(documentId, { auto_review: false });
      watchJob(job_id, "识别中 ");
      const d = await getDocument(documentId);
      setDoc(d);
    } catch (e) {
      alert(String((e as Error).message || e));
    }
  };

  const onRunReview = async () => {
    if (!documentId || ocrBusy) return;
    try {
      const { job_id } = await runReview(documentId);
      watchJob(job_id, "质检中 ");
      const d = await getDocument(documentId);
      setDoc(d);
    } catch (e) {
      alert(String((e as Error).message || e));
    }
  };

  const onStopOcr = async () => {
    if (!documentId) return;
    if (!confirm("停止当前文档的 OCR？已完成的页面会保留。")) return;
    try {
      await stopOcr(documentId);
      esRef.current?.close();
      esRef.current = null;
      setOcrBusy(false);
      setProgress(null);
      await loadArtifacts();
    } catch (e) {
      alert(String((e as Error).message || e));
    }
  };

  const onRerunPages = async () => {
    if (!documentId || ocrBusy) return;
    const raw = prompt("重跑页码（逗号分隔，如 4,5,8）");
    if (!raw) return;
    const pages = raw
      .split(/[,，\s]+/)
      .map((x) => parseInt(x, 10))
      .filter((n) => n > 0);
    if (!pages.length) return;
    try {
      const { job_id } = await rerunPages(documentId, pages, { force: true });
      watchJob(job_id, `重跑页 ${pages.join(",")} · `);
      const d = await getDocument(documentId);
      setDoc(d);
    } catch (e) {
      alert(String((e as Error).message || e));
    }
  };

  useEffect(() => {
    if (activeIdx == null) return;
    blockRefs.current.get(activeIdx)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeIdx, page]);

  if (!documentId) return null;

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-canvas">
      <header className="shrink-0 border-b border-border bg-white/90 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h1 className="truncate text-lg font-semibold text-crimson-800">{doc?.title || "工作台"}</h1>
            <p className="text-xs text-muted">PDF + bbox · Blocks / Markdown 双向定位</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {doc?.error ? (
              <button
                type="button"
                title={doc.error}
                onClick={() => setTab("review")}
                className="max-w-[14rem] truncate rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-800"
              >
                失败：{doc.error}
              </button>
            ) : null}
            {review?.score != null ? (
              <button
                type="button"
                onClick={() => setTab("review")}
                className={cn(
                  "rounded border px-2 py-1 text-xs",
                  review.needs_rerun
                    ? "border-amber-300 bg-amber-50 text-amber-900"
                    : "border-emerald-200 bg-emerald-50 text-emerald-800"
                )}
              >
                质检 {review.score.toFixed(2)}
                {review.needs_rerun ? " · 需重跑" : ""}
              </button>
            ) : null}
            <button
              type="button"
              disabled={ocrBusy}
              onClick={onRunOcr}
              className="rounded border px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-50"
            >
              {doc?.status === "partial" || doc?.status === "failed" ? "继续 OCR" : "运行 OCR"}
            </button>
            <button
              type="button"
              disabled={ocrBusy}
              onClick={onRunReview}
              className="rounded border border-sky-300 bg-sky-50 px-2 py-1 text-xs text-sky-900 hover:bg-sky-100 disabled:opacity-50"
            >
              运行质检
            </button>
            {ocrBusy || doc?.status === "ocr_running" || doc?.status === "review_running" ? (
              <button
                type="button"
                onClick={onStopOcr}
                className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900 hover:bg-amber-100"
              >
                停止
              </button>
            ) : null}
            <button
              type="button"
              disabled={ocrBusy}
              onClick={onRerunPages}
              className="rounded border px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-50"
            >
              重跑页
            </button>
            <Link to="/" className="rounded border px-2 py-1 text-xs hover:bg-crimson-50">
              ← 图书馆
            </Link>
          </div>
        </div>
        {progress ? <OcrProgressBar progress={progress} className="mt-3 max-w-xl" /> : null}
        {doc?.error && (doc.status === "failed" || doc.status === "partial") ? (
          <p className="mt-2 max-w-3xl text-xs leading-5 text-crimson-700" title={doc.error}>
            OCR 错误：{doc.error}
          </p>
        ) : null}
        {review?.summary && tab !== "review" ? (
          <p className="mt-2 max-w-3xl truncate text-xs text-muted" title={review.summary}>
            质检：{review.summary}
          </p>
        ) : null}
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-muted">加载中…</div>
      ) : error ? (
        <div className="flex flex-1 items-center justify-center text-crimson-700">{error}</div>
      ) : (
        <>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-white px-3 py-2">
            <div className="flex items-center gap-2 text-sm">
              <button
                type="button"
                className="rounded px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-40"
                disabled={page <= 1}
                onClick={() => goTo(page - 1)}
              >
                上一页
              </button>
              <label className="flex items-center gap-1 text-muted">
                <span className="sr-only">跳转到页码</span>
                <input
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  value={pageInput}
                  disabled={pageCount <= 0}
                  onChange={(e) => setPageInput(e.target.value.replace(/[^\d]/g, ""))}
                  onBlur={commitPageInput}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      (e.target as HTMLInputElement).blur();
                      commitPageInput();
                    } else if (e.key === "Escape") {
                      setPageInput(String(page));
                      (e.target as HTMLInputElement).blur();
                    }
                  }}
                  className="w-14 rounded border border-border bg-white px-1.5 py-0.5 text-center text-xs text-ink tabular-nums outline-none focus:border-crimson-400"
                  title="输入页码后回车跳转"
                  aria-label="页码"
                />
                <span className="text-xs">/ {pageCount || "—"}</span>
              </label>
              <button
                type="button"
                className="rounded px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-40"
                disabled={page >= pageCount}
                onClick={() => goTo(page + 1)}
              >
                下一页
              </button>
              {estimated ? (
                <span className="rounded border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] text-amber-900">
                  bbox 估算
                </span>
              ) : null}
            </div>
            <div className="flex gap-1 rounded-lg border border-border p-0.5">
              <button
                type="button"
                className={cn("rounded-md px-2.5 py-1 text-xs", tab === "blocks" ? "bg-crimson-50 text-crimson-800" : "text-muted")}
                onClick={() => setTab("blocks")}
              >
                Blocks
              </button>
              <button
                type="button"
                className={cn("rounded-md px-2.5 py-1 text-xs", tab === "markdown" ? "bg-crimson-50 text-crimson-800" : "text-muted")}
                onClick={() => setTab("markdown")}
              >
                Markdown
              </button>
              <button
                type="button"
                className={cn("rounded-md px-2.5 py-1 text-xs", tab === "review" ? "bg-crimson-50 text-crimson-800" : "text-muted")}
                onClick={() => setTab("review")}
              >
                质检
              </button>
              <button
                type="button"
                className={cn("rounded-md px-2.5 py-1 text-xs", tab === "delivery" ? "bg-crimson-50 text-crimson-800" : "text-muted")}
                onClick={() => setTab("delivery")}
              >
                甲方交付
              </button>
            </div>
          </div>

          <main className="flex min-h-0 flex-1">
            <section className="flex min-h-0 w-1/2 min-w-0 flex-col border-r border-border bg-white">
              <PdfReader
                pdfUrl={docPdfUrl(documentId)}
                page={page}
                pageCount={pageCount || undefined}
                hidePager
                layoutBlocks={currentBlocks}
                activeBlockIndex={activeIdx}
                onBlockClick={setActiveIdx}
              />
            </section>
            <section className="flex min-h-0 w-1/2 min-w-0 flex-col bg-white">
              {tab === "delivery" && doc ? (
                <DeliveryPanel
                  documentId={documentId}
                  document={doc}
                  onGoToPage={goTo}
                  onDocumentChange={setDoc}
                />
              ) : tab === "review" ? (
                <ReviewPanel review={review} className="min-h-0 flex-1" />
              ) : tab === "markdown" ? (
                <div className="report-markdown prose-sm min-h-0 flex-1 overflow-auto px-4 py-3">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{pageMarkdown}</ReactMarkdown>
                </div>
              ) : (
                <ul className="min-h-0 flex-1 space-y-2 overflow-auto p-3">
                  {currentBlocks.length === 0 ? (
                    <li className="text-sm text-muted">本页无块或未 OCR</li>
                  ) : (
                    currentBlocks.map((b, i) => {
                      const color = ocrBlockColor(b.type);
                      const active = activeIdx === i;
                      return (
                        <li
                          key={`${page}-${i}`}
                          ref={(el) => {
                            if (el) blockRefs.current.set(i, el);
                          }}
                        >
                          <button
                            type="button"
                            onClick={() => setActiveIdx(i)}
                            className={cn(
                              "flex w-full gap-2 rounded-xl border px-3 py-2 text-left transition",
                              active ? "border-crimson-400 bg-crimson-50/80" : "border-border bg-canvas/50 hover:border-crimson-200"
                            )}
                          >
                            <span className="mt-0.5 w-1 shrink-0 self-stretch rounded-full" style={{ backgroundColor: color.bar }} />
                            <span className="min-w-0 flex-1">
                              <span className={cn("mb-1 block text-[10px] font-semibold uppercase", color.label)}>{b.type}</span>
                              <span className="block whitespace-pre-wrap text-sm leading-6">{b.text}</span>
                            </span>
                          </button>
                        </li>
                      );
                    })
                  )}
                </ul>
              )}
            </section>
          </main>
        </>
      )}
    </div>
  );
}
