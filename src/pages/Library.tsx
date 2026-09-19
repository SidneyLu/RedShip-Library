import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  batchOcr,
  batchReview,
  createFolder,
  deleteDocument,
  deleteFolder,
  docThumbUrl,
  importPdf,
  listDocuments,
  listDocumentIds,
  listFolders,
  moveDocuments,
  processWenshi,
  getDeliveryJob,
  renameFolder,
  runOcr,
  runReview,
  stopOcr,
  scanFolder,
  subscribeScanEvents,
  updateDocument,
  type DocumentItem,
  type FolderItem,
} from "@/lib/api";
import { OperationProgress } from "@/components/OperationProgress";
import { useToast } from "@/components/Toast";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 48;

const STATUS_LABEL: Record<string, string> = {
  pending: "待 OCR",
  ocr_running: "识别中",
  ocr_done: "待质检",
  review_running: "质检中",
  ready: "已完成",
  failed: "失败",
  partial: "部分完成",
  needs_rerun: "需重跑",
};

/** null = all, "" = uncategorized, string = folder name */
type FolderFilter = null | "" | string;

type FolderDialog =
  | { mode: "create" }
  | { mode: "rename"; name: string }
  | { mode: "delete"; name: string }
  | null;

export default function LibraryPage() {
  const { showToast } = useToast();
  const [items, setItems] = useState<DocumentItem[]>([]);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [uncategorizedCount, setUncategorizedCount] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const debouncedQ = useDebouncedValue(q, 300);
  const [status, setStatus] = useState("");
  const [folderFilter, setFolderFilter] = useState<FolderFilter>(null);
  const [busy, setBusy] = useState(false);
  const [ocrStarting, setOcrStarting] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectMode, setSelectMode] = useState(false);
  const [folderDialog, setFolderDialog] = useState<FolderDialog>(null);
  const [folderInput, setFolderInput] = useState("");
  const [deleteMoveTo, setDeleteMoveTo] = useState("");
  const [bulkTarget, setBulkTarget] = useState("");
  const [scanOpen, setScanOpen] = useState(false);
  const [scanRecursive, setScanRecursive] = useState(false);
  const [scanRunOcr, setScanRunOcr] = useState(true);
  const [scanOcrPending, setScanOcrPending] = useState(false);
  const [scanAutoReview, setScanAutoReview] = useState(false);
  const [scanFolderPath, setScanFolderPath] = useState<string | null>(null);
  const [operation, setOperation] = useState<{ current: number; total: number; label: string } | null>(
    null
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [page, setPage] = useState(1);
  const [filteredTotal, setFilteredTotal] = useState(0);
  const scanEsRef = useRef<EventSource | null>(null);
  const hasLoadedRef = useRef(false);
  const filtersRef = useRef({ debouncedQ, status, folderFilter });

  const listFilter = {
    q: debouncedQ || undefined,
    status: status || undefined,
    series: typeof folderFilter === "string" && folderFilter ? folderFilter : undefined,
    uncategorized: folderFilter === "",
  };

  const refreshFolders = useCallback(async () => {
    try {
      const res = await listFolders();
      setFolders(res.folders);
      setUncategorizedCount(res.uncategorized_count);
      setTotalCount(res.total);
    } catch {
      /* ignore */
    }
  }, []);

  const refresh = useCallback(
    async (silent = false) => {
      if (!silent) setLoading(true);
      setError(null);
      try {
        const res = await listDocuments({
          ...listFilter,
          limit: PAGE_SIZE,
          offset: (page - 1) * PAGE_SIZE,
        });
        setItems(res.items);
        setFilteredTotal(res.total);
        const pageCount = Math.max(1, Math.ceil(res.total / PAGE_SIZE));
        if (page > pageCount) {
          setPage(pageCount);
        }
        await refreshFolders();
      } catch (e) {
        setError(String((e as Error).message || e));
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [debouncedQ, status, folderFilter, page, refreshFolders]
  );

  useEffect(() => {
    const prev = filtersRef.current;
    if (
      prev.debouncedQ !== debouncedQ ||
      prev.status !== status ||
      prev.folderFilter !== folderFilter
    ) {
      filtersRef.current = { debouncedQ, status, folderFilter };
      setSelected(new Set());
      if (page !== 1) {
        setPage(1);
        return;
      }
    }
    void refresh(hasLoadedRef.current);
    hasLoadedRef.current = true;
  }, [debouncedQ, status, folderFilter, page, refresh]);

  useEffect(() => {
    return () => {
      scanEsRef.current?.close();
    };
  }, []);

  const onImportPdf = async () => {
    const paths = await window.electronAPI?.openPdf();
    if (!paths?.length) return;
    setBusy(true);
    const series = typeof folderFilter === "string" && folderFilter ? folderFilter : undefined;
    let ok = 0;
    let ocrQueued = 0;
    let lastTitle = "";
    const errors: string[] = [];
    try {
      for (let i = 0; i < paths.length; i++) {
        const path = paths[i];
        const name = path.replace(/\\/g, "/").split("/").pop() || path;
        setOperation({ current: i, total: paths.length, label: `正在导入：${name}` });
        try {
          const res = await importPdf({ pdf_path: path, run_ocr: true, series });
          ok += 1;
          lastTitle = res.document.title;
          if (res.job) ocrQueued += 1;
        } catch (e) {
          errors.push(`${name}: ${(e as Error).message || e}`);
        }
      }
      setOperation({ current: paths.length, total: paths.length, label: "导入完成" });
      if (ok > 0) await refresh(true);
      if (paths.length === 1 && ok === 1) {
        showToast(`已导入「${lastTitle}」${ocrQueued ? "，OCR 已排队" : ""}`, "success");
      } else if (ok > 0) {
        showToast(
          `已导入 ${ok}/${paths.length}${ocrQueued ? ` · OCR ${ocrQueued}` : ""}${
            errors.length ? ` · 失败 ${errors.length}` : ""
          }`,
          errors.length ? "error" : "success"
        );
      } else {
        showToast(errors[0] || "导入失败", "error");
      }
    } finally {
      setBusy(false);
      setOperation(null);
    }
  };

  const onScanFolder = async () => {
    const folder = await window.electronAPI?.openFolder();
    if (!folder) return;
    setScanFolderPath(folder);
    setScanOpen(true);
  };

  const onConfirmScan = async () => {
    if (!scanFolderPath) return;
    setScanOpen(false);
    setBusy(true);
    setOperation({ current: 0, total: 0, label: "正在扫描文件夹…" });
    try {
      const { scan_job_id } = await scanFolder({
        folder_path: scanFolderPath,
        recursive: scanRecursive,
        run_ocr: scanRunOcr,
        ocr_pending_in_library: scanOcrPending,
        auto_review: scanAutoReview,
      });
      setBusy(false);

      scanEsRef.current?.close();
      scanEsRef.current = subscribeScanEvents(scan_job_id, (ev) => {
        if (ev.type === "progress") {
          setOperation({
            current: ev.current ?? 0,
            total: ev.total ?? 0,
            label: ev.file ? `扫描中：${ev.file}` : "正在扫描…",
          });
        } else if (ev.type === "done") {
          showToast(
            `扫描完成 · 导入 ${ev.count ?? 0} · 跳过重复 ${ev.skipped_sha_count ?? 0} · OCR ${ev.queued_ocr_count ?? 0}`,
            "success"
          );
          setOperation(null);
          scanEsRef.current?.close();
          scanEsRef.current = null;
          refresh(true);
        } else if (ev.type === "error") {
          showToast(ev.error || "扫描失败", "error");
          setOperation(null);
          scanEsRef.current?.close();
          scanEsRef.current = null;
        }
      });
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
      setBusy(false);
      setOperation(null);
    } finally {
      setScanFolderPath(null);
    }
  };

  const onReOcr = async (id: string) => {
    setOcrStarting((prev) => new Set(prev).add(id));
    try {
      await runOcr(id, { auto_review: false });
      showToast("OCR 已启动（不含质检）", "success");
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setOcrStarting((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const onRunReview = async (id: string) => {
    setOcrStarting((prev) => new Set(prev).add(id));
    try {
      await runReview(id);
      showToast("质检已启动", "success");
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setOcrStarting((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const onBulkOcr = async () => {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      const res = await batchOcr({
        document_ids: [...selected],
        auto_review: false,
      });
      showToast(
        `已排队 OCR ${res.count} 项` + (res.skipped.length ? ` · 跳过 ${res.skipped.length}` : ""),
        "success"
      );
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setBusy(false);
    }
  };

  const onBulkReview = async () => {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      const res = await batchReview({ document_ids: [...selected] });
      showToast(
        `已排队质检 ${res.count} 项` + (res.skipped.length ? ` · 跳过 ${res.skipped.length}` : ""),
        "success"
      );
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setBusy(false);
    }
  };

  const onBulkDelivery = async () => {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      const started = await processWenshi({
        document_ids: [...selected],
        split_papers: true,
        extract_entities: true,
        use_model: true,
        export: true,
      });
      setOperation({ current: 0, total: started.total, label: "正在生成甲方交付数据…" });
      while (true) {
        const job = await getDeliveryJob(started.job_id);
        setOperation({
          current: job.current,
          total: job.total,
          label: `篇目/实体/导出 ${job.current}/${job.total}`,
        });
        if (job.status !== "running") {
          const success = job.results.filter((item) => item.ok).length;
          const failed = job.results.length - success;
          showToast(
            `交付处理完成 ${success}/${job.total}${failed ? ` · 失败 ${failed}` : ""}`,
            failed ? "error" : "success"
          );
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setBusy(false);
      setOperation(null);
    }
  };

  const onStopOcr = async (id: string, title: string) => {
    if (!confirm(`停止「${title}」的进行中任务？已完成的页面会保留。`)) return;
    try {
      await stopOcr(id);
      showToast("OCR 已停止", "info");
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    }
  };

  const onDelete = async (id: string, title: string) => {
    if (!confirm(`从图书馆移除「${title}」？`)) return;
    await deleteDocument(id);
    showToast("已移除", "info");
    await refresh(true);
  };

  const startRename = (doc: DocumentItem) => {
    setEditingId(doc.id);
    setEditingTitle(doc.title);
  };

  const saveRename = async (id: string) => {
    const title = editingTitle.trim();
    if (!title) {
      showToast("标题不能为空", "error");
      return;
    }
    try {
      await updateDocument(id, { title });
      setEditingId(null);
      showToast("已重命名", "success");
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    }
  };

  const onMoveToFolder = async (id: string, folderName: string) => {
    try {
      await moveDocuments([id], folderName || null);
      showToast(folderName ? `已移至「${folderName}」` : "已移出文件夹", "success");
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    }
  };

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAllVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const d of items) next.add(d.id);
      return next;
    });
  };

  const selectAllFiltered = async () => {
    try {
      const res = await listDocumentIds(listFilter);
      setSelected(new Set(res.ids));
      showToast(`已选中筛选结果 ${res.ids.length} 项`, "info");
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    }
  };

  const clearSelection = () => setSelected(new Set());

  const onBulkMove = async () => {
    if (selected.size === 0) return;
    try {
      const folder = bulkTarget || null;
      await moveDocuments([...selected], folder);
      showToast(
        folder ? `已将 ${selected.size} 项移至「${folder}」` : `已将 ${selected.size} 项移出文件夹`,
        "success"
      );
      clearSelection();
      setSelectMode(false);
      await refresh(true);
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    }
  };

  const openCreateFolder = () => {
    setFolderInput("");
    setFolderDialog({ mode: "create" });
  };

  const openRenameFolder = (name: string) => {
    setFolderInput(name);
    setFolderDialog({ mode: "rename", name });
  };

  const openDeleteFolder = (name: string) => {
    setDeleteMoveTo("");
    setFolderDialog({ mode: "delete", name });
  };

  const submitFolderDialog = async () => {
    if (!folderDialog) return;
    try {
      if (folderDialog.mode === "create") {
        const name = folderInput.trim();
        if (!name) {
          showToast("请输入文件夹名称", "error");
          return;
        }
        await createFolder(name);
        showToast(`已创建「${name}」`, "success");
        setFolderDialog(null);
        await refreshFolders();
      } else if (folderDialog.mode === "rename") {
        const name = folderInput.trim();
        if (!name) {
          showToast("请输入文件夹名称", "error");
          return;
        }
        await renameFolder(folderDialog.name, name);
        if (folderFilter === folderDialog.name) setFolderFilter(name);
        showToast("文件夹已重命名", "success");
        setFolderDialog(null);
        await refresh(true);
      } else if (folderDialog.mode === "delete") {
        await deleteFolder(folderDialog.name, deleteMoveTo || undefined);
        if (folderFilter === folderDialog.name) setFolderFilter(null);
        showToast("文件夹已删除", "success");
        setFolderDialog(null);
        await refresh(true);
      }
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    }
  };

  const folderLabel =
    folderFilter === null ? "全部" : folderFilter === "" ? "未分类" : folderFilter;

  return (
    <div className="min-h-screen bg-canvas">
      <header className="border-b border-border bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4">
          <div>
            <h1 className="text-xl font-semibold text-crimson-800">OCR 图书馆</h1>
            <p className="text-xs text-muted">本地持久化 · Vision PDF OCR · DashScope VL</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onImportPdf}
              className="rounded-lg bg-crimson-700 px-3 py-1.5 text-sm text-white hover:bg-crimson-800 disabled:opacity-50"
            >
              导入 PDF
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onScanFolder}
              className="rounded-lg border border-border bg-white px-3 py-1.5 text-sm hover:bg-crimson-50"
            >
              扫描文件夹
            </button>
            <Link
              to="/search"
              className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-crimson-50"
            >
              全文检索
            </Link>
            <Link to="/settings" className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-crimson-50">
              设置
            </Link>
          </div>
        </div>
        {operation ? (
          <div className="mx-auto max-w-6xl px-4 pb-3">
            <OperationProgress
              progress={{
                current: operation.current,
                total: operation.total,
                label: operation.label,
              }}
            />
          </div>
        ) : null}
        <div className="mx-auto flex max-w-6xl flex-wrap gap-2 px-4 pb-3">
          <input
            className="rounded-lg border border-border px-3 py-1.5 text-sm"
            placeholder="搜索标题…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            className="rounded-lg border border-border px-3 py-1.5 text-sm"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">全部状态</option>
            <option value="ready">已完成</option>
            <option value="ocr_done">待质检</option>
            <option value="pending">待 OCR</option>
            <option value="ocr_running">识别中</option>
            <option value="review_running">质检中</option>
            <option value="needs_rerun">需重跑</option>
            <option value="partial">部分完成</option>
            <option value="failed">失败</option>
          </select>
          <button
            type="button"
            onClick={() => {
              setSelectMode((v) => !v);
              clearSelection();
            }}
            className={cn(
              "rounded-lg border px-3 py-1.5 text-sm",
              selectMode ? "border-crimson-400 bg-crimson-50 text-crimson-800" : "border-border hover:bg-crimson-50"
            )}
          >
            {selectMode ? "取消选择" : "选择内容"}
          </button>
          <button type="button" onClick={() => refresh()} className="rounded-lg px-3 py-1.5 text-sm text-crimson-700">
            刷新
          </button>
        </div>
      </header>

      <div className="mx-auto flex max-w-6xl gap-4 px-4 py-6">
        <aside className="w-56 shrink-0">
          <div className="rounded-xl border border-border bg-white p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">文件夹</h2>
              <button
                type="button"
                onClick={openCreateFolder}
                className="rounded border border-border px-2 py-0.5 text-xs hover:bg-crimson-50"
              >
                新建
              </button>
            </div>
            <nav className="space-y-0.5 text-sm">
              <button
                type="button"
                onClick={() => setFolderFilter(null)}
                className={cn(
                  "flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left",
                  folderFilter === null ? "bg-crimson-50 text-crimson-800" : "hover:bg-canvas"
                )}
              >
                <span>全部</span>
                <span className="text-xs text-muted">{totalCount}</span>
              </button>
              <button
                type="button"
                onClick={() => setFolderFilter("")}
                className={cn(
                  "flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left",
                  folderFilter === "" ? "bg-crimson-50 text-crimson-800" : "hover:bg-canvas"
                )}
              >
                <span>未分类</span>
                <span className="text-xs text-muted">{uncategorizedCount}</span>
              </button>
              {folders.map((f) => (
                <div
                  key={f.name}
                  className={cn(
                    "rounded-lg",
                    folderFilter === f.name ? "bg-crimson-50" : "hover:bg-canvas"
                  )}
                >
                  <button
                    type="button"
                    onClick={() => setFolderFilter(f.name)}
                    className={cn(
                      "flex w-full items-center justify-between px-2 py-1.5 text-left",
                      folderFilter === f.name ? "text-crimson-800" : ""
                    )}
                  >
                    <span className="truncate">{f.name}</span>
                    <span className="shrink-0 text-xs text-muted">{f.count}</span>
                  </button>
                  <div className="flex gap-1 px-2 pb-1.5">
                    <button
                      type="button"
                      onClick={() => openRenameFolder(f.name)}
                      className="rounded px-1.5 py-0.5 text-[11px] text-muted hover:bg-white hover:text-crimson-700"
                    >
                      重命名
                    </button>
                    <button
                      type="button"
                      onClick={() => openDeleteFolder(f.name)}
                      className="rounded px-1.5 py-0.5 text-[11px] text-muted hover:bg-white hover:text-crimson-700"
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </nav>
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted">
              当前：<span className="font-medium text-ink">{folderLabel}</span>
              {filteredTotal > 0
                ? ` · ${filteredTotal} 项${
                    filteredTotal > PAGE_SIZE
                      ? ` · 第 ${page}/${Math.max(1, Math.ceil(filteredTotal / PAGE_SIZE))} 页`
                      : ""
                  }`
                : ""}
            </p>
            {selectMode ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <button type="button" onClick={selectAllVisible} className="text-crimson-700 hover:underline">
                  全选本页
                </button>
                <button type="button" onClick={selectAllFiltered} className="text-crimson-700 hover:underline">
                  全选筛选结果
                </button>
                <button type="button" onClick={clearSelection} className="text-muted hover:underline">
                  清空
                </button>
                <span className="text-muted">已选 {selected.size}</span>
                <select
                  className="rounded border border-border px-2 py-1 text-xs"
                  value={bulkTarget}
                  onChange={(e) => setBulkTarget(e.target.value)}
                >
                  <option value="">移至未分类</option>
                  {folders.map((f) => (
                    <option key={f.name} value={f.name}>
                      移至 {f.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={selected.size === 0 || busy}
                  onClick={onBulkOcr}
                  className="rounded border border-border px-2.5 py-1 text-xs hover:bg-crimson-50 disabled:opacity-50"
                >
                  批量 OCR
                </button>
                <button
                  type="button"
                  disabled={selected.size === 0 || busy}
                  onClick={onBulkReview}
                  className="rounded border border-border px-2.5 py-1 text-xs hover:bg-crimson-50 disabled:opacity-50"
                >
                  批量质检
                </button>
                <button
                  type="button"
                  disabled={selected.size === 0 || busy}
                  onClick={onBulkDelivery}
                  className="rounded border border-emerald-300 bg-emerald-50 px-2.5 py-1 text-xs text-emerald-900 hover:bg-emerald-100 disabled:opacity-50"
                >
                  生成甲方交付包
                </button>
                <button
                  type="button"
                  disabled={selected.size === 0}
                  onClick={onBulkMove}
                  className="rounded bg-crimson-700 px-2.5 py-1 text-xs text-white hover:bg-crimson-800 disabled:opacity-50"
                >
                  应用
                </button>
              </div>
            ) : null}
          </div>

          {loading ? (
            <p className="text-sm text-muted">加载中…</p>
          ) : error ? (
            <p className="text-sm text-crimson-700">{error}</p>
          ) : items.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border bg-white p-12 text-center">
              <p className="text-muted">当前范围没有文档</p>
              <p className="mt-2 text-sm text-muted">导入 PDF，或切换到其他文件夹</p>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {items.map((doc) => {
                const checked = selected.has(doc.id);
                return (
                  <article
                    key={doc.id}
                    className={cn(
                      "relative flex flex-col rounded-xl border bg-white shadow-sm transition hover:shadow-md",
                      checked ? "border-crimson-400 ring-1 ring-crimson-200" : "border-border"
                    )}
                  >
                    {selectMode ? (
                      <label className="absolute left-2 top-2 z-10 flex cursor-pointer items-center gap-1 rounded bg-white/90 px-1.5 py-1 text-xs shadow">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleSelect(doc.id)}
                        />
                        选择
                      </label>
                    ) : null}
                    <div className="relative aspect-[4/3] overflow-hidden bg-canvas">
                      <div className="absolute inset-0 flex items-center justify-center text-xs text-muted">PDF</div>
                      <img
                        src={docThumbUrl(doc.id)}
                        alt=""
                        loading="lazy"
                        decoding="async"
                        className="relative h-full w-full object-cover object-top"
                        onError={(e) => {
                          (e.target as HTMLImageElement).style.display = "none";
                        }}
                      />
                    </div>
                    <div className="flex flex-1 flex-col gap-2 p-4">
                      <div className="flex items-start justify-between gap-2">
                        {editingId === doc.id ? (
                          <input
                            className="min-w-0 flex-1 rounded border border-border px-2 py-1 text-sm"
                            value={editingTitle}
                            autoFocus
                            onChange={(e) => setEditingTitle(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") saveRename(doc.id);
                              if (e.key === "Escape") setEditingId(null);
                            }}
                            onBlur={() => saveRename(doc.id)}
                          />
                        ) : (
                          <button
                            type="button"
                            title="点击重命名"
                            onClick={() => startRename(doc)}
                            className="line-clamp-2 text-left text-sm font-semibold text-ink hover:text-crimson-800"
                          >
                            {doc.title}
                          </button>
                        )}
                        <span
                          className={cn(
                            "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
                            doc.status === "ready"
                              ? "bg-emerald-50 text-emerald-800"
                              : doc.status === "ocr_done"
                                ? "bg-sky-50 text-sky-800"
                                : doc.status === "failed"
                                  ? "bg-red-50 text-red-700"
                                  : doc.status === "partial"
                                    ? "bg-orange-50 text-orange-800"
                                    : "bg-amber-50 text-amber-900"
                          )}
                        >
                          {STATUS_LABEL[doc.status] || doc.status}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <select
                          className="max-w-full rounded border border-border px-1.5 py-0.5 text-xs"
                          value={doc.series || ""}
                          onChange={(e) => onMoveToFolder(doc.id, e.target.value)}
                          title="归类到文件夹"
                          disabled={selectMode}
                        >
                          <option value="">未分类</option>
                          {folders.map((f) => (
                            <option key={f.name} value={f.name}>
                              {f.name}
                            </option>
                          ))}
                        </select>
                      </div>
                      <p className="text-xs text-muted">
                        {doc.pages} 页 · {doc.block_count} 块
                        {doc.review_score != null ? ` · 质检 ${doc.review_score.toFixed(2)}` : ""}
                      </p>
                      {doc.review_summary ? (
                        <p className="line-clamp-2 text-xs leading-5 text-ink/80" title={doc.review_summary}>
                          {doc.review_summary}
                        </p>
                      ) : null}
                      {doc.error && (doc.status === "failed" || doc.status === "partial") ? (
                        <p className="line-clamp-2 text-xs leading-5 text-crimson-700" title={doc.error}>
                          失败原因：{doc.error}
                        </p>
                      ) : null}
                      <div className="mt-auto flex flex-wrap gap-2 pt-2">
                        <Link
                          to={`/workbench/${doc.id}`}
                          className="rounded bg-crimson-700 px-2.5 py-1 text-xs text-white hover:bg-crimson-800"
                        >
                          打开
                        </Link>
                        {doc.status === "ocr_running" ? (
                          <button
                            type="button"
                            onClick={() => onStopOcr(doc.id, doc.title)}
                            className="rounded border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs text-amber-900 hover:bg-amber-100"
                          >
                            停止 OCR
                          </button>
                        ) : doc.status === "review_running" ? (
                          <button
                            type="button"
                            onClick={() => onStopOcr(doc.id, doc.title)}
                            className="rounded border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs text-amber-900 hover:bg-amber-100"
                          >
                            停止质检
                          </button>
                        ) : (
                          <>
                            <button
                              type="button"
                              disabled={ocrStarting.has(doc.id)}
                              onClick={() => onReOcr(doc.id)}
                              className="rounded border border-border px-2.5 py-1 text-xs hover:bg-crimson-50 disabled:opacity-50"
                            >
                              {doc.status === "partial" || doc.status === "failed"
                                ? "继续 OCR"
                                : "重新 OCR"}
                            </button>
                            {(doc.status === "ocr_done" ||
                              doc.status === "needs_rerun" ||
                              doc.status === "ready") && (
                              <button
                                type="button"
                                disabled={ocrStarting.has(doc.id)}
                                onClick={() => onRunReview(doc.id)}
                                className="rounded border border-sky-300 bg-sky-50 px-2.5 py-1 text-xs text-sky-900 hover:bg-sky-100 disabled:opacity-50"
                              >
                                质检
                              </button>
                            )}
                          </>
                        )}
                        <button
                          type="button"
                          onClick={() => onDelete(doc.id, doc.title)}
                          className="rounded px-2.5 py-1 text-xs text-muted hover:text-crimson-700"
                        >
                          移除
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
          {filteredTotal > PAGE_SIZE ? (
            <div className="mt-4 flex flex-wrap items-center justify-center gap-2 text-sm">
              <button
                type="button"
                disabled={page <= 1 || loading}
                onClick={() => setPage(1)}
                className="rounded border border-border px-2.5 py-1 hover:bg-crimson-50 disabled:opacity-40"
              >
                首页
              </button>
              <button
                type="button"
                disabled={page <= 1 || loading}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded border border-border px-2.5 py-1 hover:bg-crimson-50 disabled:opacity-40"
              >
                上一页
              </button>
              <span className="px-2 text-muted">
                {page} / {Math.max(1, Math.ceil(filteredTotal / PAGE_SIZE))}
              </span>
              <button
                type="button"
                disabled={page >= Math.ceil(filteredTotal / PAGE_SIZE) || loading}
                onClick={() =>
                  setPage((p) => Math.min(Math.ceil(filteredTotal / PAGE_SIZE), p + 1))
                }
                className="rounded border border-border px-2.5 py-1 hover:bg-crimson-50 disabled:opacity-40"
              >
                下一页
              </button>
              <button
                type="button"
                disabled={page >= Math.ceil(filteredTotal / PAGE_SIZE) || loading}
                onClick={() => setPage(Math.max(1, Math.ceil(filteredTotal / PAGE_SIZE)))}
                className="rounded border border-border px-2.5 py-1 hover:bg-crimson-50 disabled:opacity-40"
              >
                末页
              </button>
            </div>
          ) : null}
        </main>
      </div>

      {folderDialog ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-sm rounded-xl border border-border bg-white p-5 shadow-lg">
            <h2 className="text-base font-semibold text-crimson-800">
              {folderDialog.mode === "create"
                ? "新建文件夹"
                : folderDialog.mode === "rename"
                  ? "重命名文件夹"
                  : "删除文件夹"}
            </h2>
            {folderDialog.mode === "delete" ? (
              <div className="mt-3 space-y-3 text-sm">
                <p className="text-muted">
                  删除「{folderDialog.name}」后，其中的文档将移到：
                </p>
                <select
                  className="w-full rounded border border-border px-3 py-2"
                  value={deleteMoveTo}
                  onChange={(e) => setDeleteMoveTo(e.target.value)}
                >
                  <option value="">未分类</option>
                  {folders
                    .filter((f) => f.name !== folderDialog.name)
                    .map((f) => (
                      <option key={f.name} value={f.name}>
                        {f.name}
                      </option>
                    ))}
                </select>
              </div>
            ) : (
              <input
                className="mt-3 w-full rounded border border-border px-3 py-2 text-sm"
                placeholder="文件夹名称"
                value={folderInput}
                autoFocus
                onChange={(e) => setFolderInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submitFolderDialog();
                }}
              />
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setFolderDialog(null)}
                className="rounded border border-border px-3 py-1.5 text-sm hover:bg-crimson-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submitFolderDialog}
                className={cn(
                  "rounded px-3 py-1.5 text-sm text-white",
                  folderDialog.mode === "delete"
                    ? "bg-red-600 hover:bg-red-700"
                    : "bg-crimson-700 hover:bg-crimson-800"
                )}
              >
                {folderDialog.mode === "delete" ? "删除" : "确定"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {scanOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-md rounded-xl border border-border bg-white p-5 shadow-lg">
            <h2 className="text-base font-semibold text-crimson-800">扫描文件夹</h2>
            <p className="mt-1 truncate text-xs text-muted">{scanFolderPath}</p>
            <div className="mt-4 space-y-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={scanRecursive}
                  onChange={(e) => setScanRecursive(e.target.checked)}
                />
                递归扫描子目录
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={scanRunOcr}
                  onChange={(e) => setScanRunOcr(e.target.checked)}
                />
                扫描后自动 OCR 新导入文档
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={scanOcrPending}
                  onChange={(e) => setScanOcrPending(e.target.checked)}
                />
                同时 OCR 库内待处理文档（pending / partial / failed）
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={scanAutoReview}
                  onChange={(e) => setScanAutoReview(e.target.checked)}
                  disabled={!scanRunOcr && !scanOcrPending}
                />
                OCR 后自动质检（默认关，可稍后批量质检）
              </label>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setScanOpen(false);
                  setScanFolderPath(null);
                }}
                className="rounded border border-border px-3 py-1.5 text-sm hover:bg-crimson-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={onConfirmScan}
                className="rounded bg-crimson-700 px-3 py-1.5 text-sm text-white hover:bg-crimson-800"
              >
                开始扫描
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
