import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getSettings,
  mergeExternalDocs,
  reindexLibrary,
  updateSettings,
  type SettingsData,
} from "@/lib/api";
import { OperationProgress } from "@/components/OperationProgress";
import { useToast } from "@/components/Toast";

export default function SettingsPage() {
  const { showToast } = useToast();
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://dashscope.aliyuncs.com/api/v1");
  const [dpi, setDpi] = useState(300);
  const [maxPages, setMaxPages] = useState(1000);
  const [visionModel, setVisionModel] = useState("qwen3.5-flash");
  const [pageConcurrency, setPageConcurrency] = useState(16);
  const [docConcurrency, setDocConcurrency] = useState(8);
  const [apiConcurrency, setApiConcurrency] = useState(128);
  const [workerProcesses, setWorkerProcesses] = useState(1);
  const [dataRoot, setDataRoot] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [libraryBusy, setLibraryBusy] = useState(false);
  const [operation, setOperation] = useState<{ current: number; total: number; label: string } | null>(
    null
  );

  useEffect(() => {
    getSettings().then((s) => {
      setSettings(s);
      setBaseUrl(s.dashscope_http_api_url || "https://dashscope.aliyuncs.com/api/v1");
      setDpi(s.vision_pdf_dpi);
      setMaxPages(s.vision_pdf_max_pages);
      setVisionModel(s.vision_model);
      setPageConcurrency(s.ocr_page_concurrency);
      setDocConcurrency(s.ocr_document_concurrency);
      setApiConcurrency(s.ocr_api_concurrency);
      setWorkerProcesses(s.ocr_worker_processes ?? 1);
      setDataRoot(s.data_root);
    });
  }, []);

  const onPickDataRoot = async () => {
    const folder = await window.electronAPI?.openFolder();
    if (folder) setDataRoot(folder);
  };

  const onSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const clamp = (n: number, min: number, max: number, fallback: number) => {
        if (!Number.isFinite(n)) return fallback;
        return Math.min(max, Math.max(min, Math.trunc(n)));
      };
      const body: Record<string, unknown> = {
        vision_pdf_dpi: clamp(dpi, 72, 600, 300),
        vision_pdf_max_pages: clamp(maxPages, 1, 10000, 1000),
        vision_model: visionModel,
        ocr_page_concurrency: clamp(pageConcurrency, 1, 64, 16),
        ocr_document_concurrency: clamp(docConcurrency, 1, 32, 8),
        ocr_api_concurrency: clamp(apiConcurrency, 1, 256, 128),
        ocr_worker_processes: clamp(workerProcesses, 1, 16, 1),
      };
      if (dataRoot) body.data_root = dataRoot;
      if (baseUrl.trim()) body.dashscope_http_api_url = baseUrl.trim().replace(/\/$/, "");
      if (apiKey.trim()) body.dashscope_api_key = apiKey.trim();
      const s = await updateSettings(body);
      setSettings(s);
      setDpi(s.vision_pdf_dpi);
      setMaxPages(s.vision_pdf_max_pages);
      setVisionModel(s.vision_model);
      setPageConcurrency(s.ocr_page_concurrency);
      setDocConcurrency(s.ocr_document_concurrency);
      setApiConcurrency(s.ocr_api_concurrency);
      setWorkerProcesses(s.ocr_worker_processes ?? 1);
      setDataRoot(s.data_root);
      setApiKey("");
      setMessage("已保存");
      showToast("设置已保存", "success");
    } catch (e) {
      const msg = String((e as Error).message || e);
      setMessage(msg);
      showToast(msg, "error");
    } finally {
      setSaving(false);
    }
  };

  const onReindex = async () => {
    setLibraryBusy(true);
    setMessage(null);
    setOperation({ current: 0, total: 1, label: "正在重建索引…" });
    try {
      const report = await reindexLibrary();
      const msg = `重建索引完成：新增 ${report.added_count} · 更新 ${report.updated_count} · 重复跳过 ${report.skipped_duplicate_sha.length}`;
      setMessage(msg);
      showToast(msg, "success");
    } catch (e) {
      const msg = String((e as Error).message || e);
      setMessage(msg);
      showToast(msg, "error");
    } finally {
      setLibraryBusy(false);
      setOperation(null);
    }
  };

  const onMergeDocs = async () => {
    const folder = await window.electronAPI?.openFolder();
    if (!folder) return;
    setLibraryBusy(true);
    setMessage(null);
    setOperation({ current: 0, total: 1, label: "正在合并 docs…" });
    try {
      const report = await mergeExternalDocs({ source_path: folder, run_reindex: true });
      const reindex = report.reindex;
      const msg =
        `合并完成：复制 ${report.copied_count} · 相同跳过 ${report.skipped_same.length} · 冲突 ${report.conflicts.length}` +
        (reindex
          ? ` · 索引新增 ${reindex.added_count} 更新 ${reindex.updated_count}`
          : "");
      setMessage(msg);
      showToast(msg, "success");
    } catch (e) {
      const msg = String((e as Error).message || e);
      setMessage(msg);
      showToast(msg, "error");
    } finally {
      setLibraryBusy(false);
      setOperation(null);
    }
  };

  return (
    <div className="min-h-screen bg-canvas px-4 py-8">
      <div className="mx-auto max-w-lg rounded-xl border border-border bg-white p-6 shadow-sm">
        <div className="mb-6 flex items-center justify-between">
          <h1 className="text-lg font-semibold text-crimson-800">设置</h1>
          <Link to="/" className="text-sm text-crimson-700">← 图书馆</Link>
        </div>

        <div className="space-y-4 text-sm">
          {operation ? (
            <OperationProgress
              progress={{
                current: operation.current,
                total: operation.total,
                label: operation.label,
              }}
            />
          ) : null}
          <label className="block">
            <span className="text-muted">数据目录（可移植图书馆根目录）</span>
            <div className="mt-1 flex gap-2">
              <input className="flex-1 rounded border border-border px-3 py-2" value={dataRoot} onChange={(e) => setDataRoot(e.target.value)} />
              <button type="button" onClick={onPickDataRoot} className="rounded border px-3 py-2 hover:bg-crimson-50">选择</button>
            </div>
            <span className="mt-1 block text-xs leading-5 text-muted">
              换机使用：拷贝整个 Library 文件夹到新电脑 → 安装本应用 → 在此选择该文件夹并保存。
              目录内需含 library.db、docs/；API Key 会保存在同目录 secrets.json。
              若列表不全，点下方「从 docs/ 重建索引」。
            </span>
          </label>

          <label className="block">
            <span className="text-muted">DashScope API Key {settings?.has_api_key ? "（已配置，留空则不修改）" : ""}</span>
            <input
              type="password"
              className="mt-1 w-full rounded border border-border px-3 py-2"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
            />
          </label>

          <label className="block">
            <span className="text-muted">DashScope Base URL</span>
            <input
              className="mt-1 w-full rounded border border-border px-3 py-2"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://dashscope.aliyuncs.com/api/v1"
            />
            <span className="mt-1 block text-xs text-muted">
              默认北京；新加坡可用 https://dashscope-intl.aliyuncs.com/api/v1
            </span>
          </label>

          <label className="block">
            <span className="text-muted">Vision 模型</span>
            <input className="mt-1 w-full rounded border border-border px-3 py-2" value={visionModel} onChange={(e) => setVisionModel(e.target.value)} />
          </label>

          <label className="block">
            <span className="text-muted">默认 DPI</span>
            <input type="number" className="mt-1 w-full rounded border border-border px-3 py-2" value={dpi} onChange={(e) => setDpi(Number(e.target.value))} />
          </label>

          <label className="block">
            <span className="text-muted">单文档最大页数</span>
            <input type="number" className="mt-1 w-full rounded border border-border px-3 py-2" value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))} />
          </label>

          <label className="block">
            <span className="text-muted">单文档页级并发（同时 OCR 的页数）</span>
            <input type="number" min={1} max={32} className="mt-1 w-full rounded border border-border px-3 py-2" value={pageConcurrency} onChange={(e) => setPageConcurrency(Number(e.target.value))} />
          </label>

          <label className="block">
            <span className="text-muted">多文档并发（同时 OCR 的 PDF 数量）</span>
            <input type="number" min={1} max={16} className="mt-1 w-full rounded border border-border px-3 py-2" value={docConcurrency} onChange={(e) => setDocConcurrency(Number(e.target.value))} />
          </label>

          <label className="block">
            <span className="text-muted">全局 API 并发上限（遇 429 自动降速）</span>
            <input type="number" min={1} max={128} className="mt-1 w-full rounded border border-border px-3 py-2" value={apiConcurrency} onChange={(e) => setApiConcurrency(Number(e.target.value))} />
            <span className="mt-1 block text-xs text-muted">当前页级×文档=128；遇 429 会自动降速</span>
          </label>

          <label className="block">
            <span className="text-muted">OCR Worker 进程数（1=进程内；2–4=多进程池）</span>
            <input
              type="number"
              min={1}
              max={8}
              className="mt-1 w-full rounded border border-border px-3 py-2"
              value={workerProcesses}
              onChange={(e) => setWorkerProcesses(Number(e.target.value))}
            />
            <span className="mt-1 block text-xs text-muted">
              大于 1 时 API 进程只调度页任务，Worker 负责渲染与 VL；全局 API 并发仍受上方上限约束
            </span>
          </label>

          <button
            type="button"
            disabled={saving}
            onClick={onSave}
            className="w-full rounded-lg bg-crimson-700 py-2 text-white hover:bg-crimson-800 disabled:opacity-50"
          >
            保存
          </button>

          <div className="border-t border-border pt-4">
            <h2 className="mb-2 font-medium text-crimson-800">多机合并 / 索引</h2>
            <p className="mb-3 text-xs text-muted">
              从另一台机器拷贝 docs/ 子目录后，在此合并并重建索引；合并后可在图书馆批量 OCR。
            </p>
            <div className="flex flex-col gap-2">
              <button
                type="button"
                disabled={libraryBusy}
                onClick={onReindex}
                className="rounded-lg border border-border py-2 hover:bg-crimson-50 disabled:opacity-50"
              >
                从 docs/ 重建索引
              </button>
              <button
                type="button"
                disabled={libraryBusy}
                onClick={onMergeDocs}
                className="rounded-lg border border-border py-2 hover:bg-crimson-50 disabled:opacity-50"
              >
                合并外部 docs 文件夹
              </button>
            </div>
          </div>

          {message ? <p className="text-center text-xs text-muted">{message}</p> : null}
        </div>
      </div>
    </div>
  );
}
