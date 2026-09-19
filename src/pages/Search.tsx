import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  getSearchIndexStatus,
  listFolders,
  searchLibrary,
  startSearchReindex,
  type FolderItem,
  type SearchHit,
} from "@/lib/api";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useToast } from "@/components/Toast";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;

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

function renderSnippet(snippet: string) {
  const parts = snippet.split(/(«[^»]*»)/g);
  return parts.map((part, i) => {
    if (part.startsWith("«") && part.endsWith("»")) {
      return (
        <mark key={i} className="rounded bg-amber-100 px-0.5 text-amber-950">
          {part.slice(1, -1)}
        </mark>
      );
    }
    return <span key={i}>{part}</span>;
  });
}

export default function SearchPage() {
  const { showToast } = useToast();
  const [params, setParams] = useSearchParams();
  const initialQ = params.get("q") || "";
  const [q, setQ] = useState(initialQ);
  const debouncedQ = useDebouncedValue(q, 300);
  const [status, setStatus] = useState("");
  const [folder, setFolder] = useState<string | null>(null);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [items, setItems] = useState<SearchHit[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [indexRows, setIndexRows] = useState(0);
  const [reindexBusy, setReindexBusy] = useState(false);

  useEffect(() => {
    listFolders()
      .then((res) => setFolders(res.folders))
      .catch(() => undefined);
    getSearchIndexStatus()
      .then((s) => setIndexRows(s.index_rows))
      .catch(() => undefined);
  }, []);

  const runSearch = useCallback(async () => {
    if (!debouncedQ.trim()) {
      setItems([]);
      setTotal(0);
      setHint(null);
      return;
    }
    setLoading(true);
    try {
      const res = await searchLibrary({
        q: debouncedQ.trim(),
        status: status || undefined,
        series: folder || undefined,
        uncategorized: folder === "",
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      setItems(res.items);
      setTotal(res.total);
      setHint(res.hint || null);
      if (typeof res.index_rows === "number") setIndexRows(res.index_rows);
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        if (debouncedQ.trim()) next.set("q", debouncedQ.trim());
        else next.delete("q");
        return next;
      });
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setLoading(false);
    }
  }, [debouncedQ, status, folder, page, setParams, showToast]);

  useEffect(() => {
    setPage(1);
  }, [debouncedQ, status, folder]);

  useEffect(() => {
    void runSearch();
  }, [runSearch]);

  const onRebuild = async () => {
    setReindexBusy(true);
    try {
      await startSearchReindex(true);
      showToast("已开始重建全文索引", "info");
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const s = await getSearchIndexStatus();
        setIndexRows(s.index_rows);
        if (s.reindex.status === "done") {
          showToast(
            `索引重建完成 · ${s.reindex.indexed} 本 · ${s.index_rows} 页行`,
            "success"
          );
          void runSearch();
          break;
        }
        if (s.reindex.status === "error") {
          showToast(s.reindex.error || "重建失败", "error");
          break;
        }
      }
    } catch (e) {
      showToast(String((e as Error).message || e), "error");
    } finally {
      setReindexBusy(false);
    }
  };

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="min-h-screen bg-canvas">
      <header className="border-b border-border bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 px-4 py-4">
          <div>
            <h1 className="text-xl font-semibold text-crimson-800">全文检索</h1>
            <p className="text-xs text-muted">
              搜索 OCR 正文（content.md）· 索引行 {indexRows.toLocaleString()}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link
              to="/"
              className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-crimson-50"
            >
              图书馆
            </Link>
            <Link
              to="/settings"
              className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-crimson-50"
            >
              设置
            </Link>
          </div>
        </div>
        <div className="mx-auto flex max-w-4xl flex-wrap gap-2 px-4 pb-3">
          <input
            className="min-w-[12rem] flex-1 rounded-lg border border-border px-3 py-1.5 text-sm"
            placeholder="输入关键词（至少 3 字）…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            autoFocus
          />
          <select
            className="rounded-lg border border-border px-3 py-1.5 text-sm"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">全部状态</option>
            <option value="ready">已完成</option>
            <option value="ocr_done">待质检</option>
            <option value="needs_rerun">需重跑</option>
            <option value="partial">部分完成</option>
          </select>
          <select
            className="rounded-lg border border-border px-3 py-1.5 text-sm"
            value={folder === null ? "__all__" : folder === "" ? "__none__" : folder}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "__all__") setFolder(null);
              else if (v === "__none__") setFolder("");
              else setFolder(v);
            }}
          >
            <option value="__all__">全部文件夹</option>
            <option value="__none__">未分类</option>
            {folders.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name}
              </option>
            ))}
          </select>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-6">
        {indexRows === 0 ? (
          <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">
            <p>全文索引为空。已完成 OCR 的文档需要重建索引后才能检索正文。</p>
            <button
              type="button"
              disabled={reindexBusy}
              onClick={onRebuild}
              className="mt-3 rounded bg-crimson-700 px-3 py-1.5 text-xs text-white hover:bg-crimson-800 disabled:opacity-50"
            >
              {reindexBusy ? "重建中…" : "重建检索索引"}
            </button>
          </div>
        ) : null}

        {loading ? (
          <p className="text-sm text-muted">检索中…</p>
        ) : hint ? (
          <p className="text-sm text-muted">{hint}</p>
        ) : !debouncedQ.trim() ? (
          <p className="text-sm text-muted">输入关键词搜索 OCR 正文与标题</p>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted">没有匹配结果</p>
        ) : (
          <>
            <p className="mb-3 text-sm text-muted">
              共 {total} 条命中
              {total > PAGE_SIZE ? ` · 第 ${page}/${pageCount} 页` : ""}
            </p>
            <ul className="space-y-3">
              {items.map((hit) => (
                <li key={`${hit.document_id}-${hit.page}-${hit.snippet.slice(0, 24)}`}>
                  <Link
                    to={`/workbench/${hit.document_id}?page=${hit.page}&q=${encodeURIComponent(debouncedQ.trim())}`}
                    className="block rounded-xl border border-border bg-white p-4 shadow-sm transition hover:border-crimson-300 hover:shadow-md"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <h2 className="text-sm font-semibold text-ink">{hit.title}</h2>
                      <span
                        className={cn(
                          "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
                          hit.status === "ready"
                            ? "bg-emerald-50 text-emerald-800"
                            : "bg-amber-50 text-amber-900"
                        )}
                      >
                        {STATUS_LABEL[hit.status] || hit.status}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-muted">
                      {hit.series || "未分类"} · 第 {hit.page} 页
                    </p>
                    <p className="mt-2 text-sm leading-6 text-ink/90">
                      {renderSnippet(hit.snippet)}
                    </p>
                  </Link>
                </li>
              ))}
            </ul>
            {total > PAGE_SIZE ? (
              <div className="mt-4 flex flex-wrap items-center justify-center gap-2 text-sm">
                <button
                  type="button"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  className="rounded border border-border px-2.5 py-1 hover:bg-crimson-50 disabled:opacity-40"
                >
                  上一页
                </button>
                <span className="text-muted">
                  {page} / {pageCount}
                </span>
                <button
                  type="button"
                  disabled={page >= pageCount}
                  onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
                  className="rounded border border-border px-2.5 py-1 hover:bg-crimson-50 disabled:opacity-40"
                >
                  下一页
                </button>
              </div>
            ) : null}
          </>
        )}
      </main>
    </div>
  );
}
