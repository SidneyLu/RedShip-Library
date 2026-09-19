import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getSettings,
  getSearchIndexStatus,
  mergeExternalDocs,
  reindexLibrary,
  startSearchReindex,
  updateSettings,
  type SettingsData,
} from "@/lib/api";
import { OperationProgress } from "@/components/OperationProgress";
import { useToast } from "@/components/Toast";

const DASHSCOPE_DEFAULT_URL = "https://dashscope.aliyuncs.com/api/v1";
const OPENAI_DEFAULT_URL = "http://127.0.0.1:8000/v1";

type Provider = "dashscope" | "openai_responses";

export default function SettingsPage() {
  const { showToast } = useToast();
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [provider, setProvider] = useState<Provider>("dashscope");
  const [dashApiKey, setDashApiKey] = useState("");
  const [dashBaseUrl, setDashBaseUrl] = useState(DASHSCOPE_DEFAULT_URL);
  const [openaiApiKey, setOpenaiApiKey] = useState("");
  const [openaiBaseUrl, setOpenaiBaseUrl] = useState(OPENAI_DEFAULT_URL);
  const [dashVisionModel, setDashVisionModel] = useState("qwen3.5-flash");
  const [dashChatModel, setDashChatModel] = useState("qwen3.5-flash");
  const [openaiVisionModel, setOpenaiVisionModel] = useState("");
  const [openaiChatModel, setOpenaiChatModel] = useState("");
  const [dpi, setDpi] = useState(300);
  const [maxPages, setMaxPages] = useState(10000);
  const [pageConcurrency, setPageConcurrency] = useState(16);
  const [docConcurrency, setDocConcurrency] = useState(8);
  const [apiConcurrency, setApiConcurrency] = useState(128);
  const [workerProcesses, setWorkerProcesses] = useState(1);
  const [deliverySubmitter, setDeliverySubmitter] = useState("");
  const [dataRoot, setDataRoot] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [libraryBusy, setLibraryBusy] = useState(false);
  const [operation, setOperation] = useState<{ current: number; total: number; label: string } | null>(
    null
  );

  const applySettings = (s: SettingsData) => {
    setSettings(s);
    const p = (s.llm_provider === "openai_responses" ? "openai_responses" : "dashscope") as Provider;
    setProvider(p);
    setDashBaseUrl(s.dashscope_http_api_url || DASHSCOPE_DEFAULT_URL);
    setOpenaiBaseUrl(s.openai_base_url || OPENAI_DEFAULT_URL);
    setDashVisionModel(s.vision_model || "qwen3.5-flash");
    setDashChatModel(s.chat_model || s.vision_model || "qwen3.5-flash");
    setOpenaiVisionModel(s.openai_vision_model || "");
    setOpenaiChatModel(s.openai_chat_model || "");
    setDpi(s.vision_pdf_dpi);
    setMaxPages(s.vision_pdf_max_pages);
    setPageConcurrency(s.ocr_page_concurrency);
    setDocConcurrency(s.ocr_document_concurrency);
    setApiConcurrency(s.ocr_api_concurrency);
    setWorkerProcesses(s.ocr_worker_processes ?? 1);
    setDeliverySubmitter(s.delivery_submitter ?? "");
    setDataRoot(s.data_root);
  };

  useEffect(() => {
    getSettings().then(applySettings);
  }, []);

  const onPickDataRoot = async () => {
    const folder = await window.electronAPI?.openFolder();
    if (folder) setDataRoot(folder);
  };

  const switchToOpenAI = () => {
    setProvider("openai_responses");
    if (!openaiBaseUrl.trim()) setOpenaiBaseUrl(OPENAI_DEFAULT_URL);
    setMessage("已切换到 OpenAI Responses（保存后生效）");
  };

  const switchToDashScope = () => {
    setProvider("dashscope");
    if (!dashBaseUrl.trim()) setDashBaseUrl(DASHSCOPE_DEFAULT_URL);
    setMessage("已切换到 DashScope（保存后生效）");
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
        llm_provider: provider,
        vision_pdf_dpi: clamp(dpi, 72, 600, 300),
        vision_pdf_max_pages: clamp(maxPages, 1, 50000, 10000),
        vision_model: dashVisionModel.trim() || "qwen3.5-flash",
        chat_model: dashChatModel.trim() || dashVisionModel.trim() || "qwen3.5-flash",
        openai_vision_model: openaiVisionModel.trim(),
        openai_chat_model: openaiChatModel.trim(),
        ocr_page_concurrency: clamp(pageConcurrency, 1, 64, 16),
        ocr_document_concurrency: clamp(docConcurrency, 1, 32, 8),
        ocr_api_concurrency: clamp(apiConcurrency, 1, 256, 128),
        ocr_worker_processes: clamp(workerProcesses, 1, 16, 1),
        delivery_submitter: deliverySubmitter.trim(),
      };
      if (dataRoot) body.data_root = dataRoot;
      if (dashBaseUrl.trim()) body.dashscope_http_api_url = dashBaseUrl.trim().replace(/\/$/, "");
      if (openaiBaseUrl.trim()) body.openai_base_url = openaiBaseUrl.trim().replace(/\/$/, "");
      if (dashApiKey.trim()) body.dashscope_api_key = dashApiKey.trim();
      if (openaiApiKey.trim()) body.openai_api_key = openaiApiKey.trim();
      const s = await updateSettings(body);
      applySettings(s);
      setDashApiKey("");
      setOpenaiApiKey("");
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

  const onSearchReindex = async () => {
    setLibraryBusy(true);
    setMessage(null);
    setOperation({ current: 0, total: 0, label: "正在重建全文检索索引…" });
    try {
      await startSearchReindex(true);
      for (let i = 0; i < 600; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        const s = await getSearchIndexStatus();
        setOperation({
          current: s.reindex.current,
          total: Math.max(1, s.reindex.total),
          label: `全文索引 ${s.reindex.current}/${s.reindex.total}（已索引 ${s.reindex.indexed}）`,
        });
        if (s.reindex.status === "done") {
          const msg = `全文索引完成：${s.reindex.indexed} 本 · ${s.index_rows} 页行 · 跳过 ${s.reindex.skipped}`;
          setMessage(msg);
          showToast(msg, "success");
          break;
        }
        if (s.reindex.status === "error") {
          throw new Error(s.reindex.error || "重建失败");
        }
      }
    } catch (e) {
      const msg = String((e as Error).message || e);
      setMessage(msg);
      showToast(msg, "error");
    } finally {
      setLibraryBusy(false);
      setOperation(null);
    }
  };

  const isOpenAI = provider === "openai_responses";

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

          <div className="rounded-lg border border-border bg-canvas/60 p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="font-medium text-crimson-800">
                当前：{isOpenAI ? "OpenAI Responses" : "DashScope"}
              </span>
              {isOpenAI ? (
                <button
                  type="button"
                  onClick={switchToDashScope}
                  className="rounded border border-border px-2.5 py-1 text-xs hover:bg-white"
                >
                  切换回 DashScope
                </button>
              ) : (
                <button
                  type="button"
                  onClick={switchToOpenAI}
                  className="rounded bg-crimson-700 px-2.5 py-1 text-xs text-white hover:bg-crimson-800"
                >
                  一键切到 OpenAI Responses
                </button>
              )}
            </div>
            <p className="text-xs leading-5 text-muted">
              Responses 面向本机 vLLM / OpenAI 兼容端点（需视觉模型支持图文）。两侧凭证独立保存，切换不互相覆盖。
            </p>
          </div>

          {isOpenAI ? (
            <>
              <label className="block">
                <span className="text-muted">
                  OpenAI API Key{" "}
                  {settings?.has_openai_api_key ? "（已配置，留空则不修改；本地可空）" : "（本地 vLLM 可留空）"}
                </span>
                <input
                  type="password"
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={openaiApiKey}
                  onChange={(e) => setOpenaiApiKey(e.target.value)}
                  placeholder="EMPTY 或 sk-..."
                />
              </label>
              <label className="block">
                <span className="text-muted">OpenAI Base URL</span>
                <input
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={openaiBaseUrl}
                  onChange={(e) => setOpenaiBaseUrl(e.target.value)}
                  placeholder={OPENAI_DEFAULT_URL}
                />
                <span className="mt-1 block text-xs text-muted">
                  默认 {OPENAI_DEFAULT_URL}；请求走 /v1/responses
                </span>
              </label>
              <label className="block">
                <span className="text-muted">Vision 模型（OCR）</span>
                <input
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={openaiVisionModel}
                  onChange={(e) => setOpenaiVisionModel(e.target.value)}
                  placeholder="vLLM 上的视觉模型名"
                />
              </label>
              <label className="block">
                <span className="text-muted">Chat 模型（质检）</span>
                <input
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={openaiChatModel}
                  onChange={(e) => setOpenaiChatModel(e.target.value)}
                  placeholder="可与 Vision 相同，或另指定文本模型"
                />
              </label>
            </>
          ) : (
            <>
              <label className="block">
                <span className="text-muted">
                  DashScope API Key {settings?.has_api_key ? "（已配置，留空则不修改）" : ""}
                </span>
                <input
                  type="password"
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={dashApiKey}
                  onChange={(e) => setDashApiKey(e.target.value)}
                  placeholder="sk-..."
                />
              </label>
              <label className="block">
                <span className="text-muted">DashScope Base URL</span>
                <input
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={dashBaseUrl}
                  onChange={(e) => setDashBaseUrl(e.target.value)}
                  placeholder={DASHSCOPE_DEFAULT_URL}
                />
                <span className="mt-1 block text-xs text-muted">
                  默认北京；新加坡可用 https://dashscope-intl.aliyuncs.com/api/v1
                </span>
              </label>
              <label className="block">
                <span className="text-muted">Vision 模型（OCR）</span>
                <input
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={dashVisionModel}
                  onChange={(e) => setDashVisionModel(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-muted">Chat 模型（质检）</span>
                <input
                  className="mt-1 w-full rounded border border-border px-3 py-2"
                  value={dashChatModel}
                  onChange={(e) => setDashChatModel(e.target.value)}
                />
              </label>
            </>
          )}

          <label className="block">
            <span className="text-muted">默认 DPI</span>
            <input type="number" className="mt-1 w-full rounded border border-border px-3 py-2" value={dpi} onChange={(e) => setDpi(Number(e.target.value))} />
          </label>

          <label className="block">
            <span className="text-muted">单文档最大页数</span>
            <input type="number" className="mt-1 w-full rounded border border-border px-3 py-2" value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))} />
          </label>

          <label className="block">
            <span className="text-muted">甲方交付提交人</span>
            <input
              className="mt-1 w-full rounded border border-border px-3 py-2"
              value={deliverySubmitter}
              onChange={(e) => setDeliverySubmitter(e.target.value)}
              placeholder="用于 提交/<姓名>/OCR 与实体目录"
            />
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
              <button
                type="button"
                disabled={libraryBusy}
                onClick={onSearchReindex}
                className="rounded-lg border border-border py-2 hover:bg-crimson-50 disabled:opacity-50"
              >
                重建全文检索索引
              </button>
            </div>
          </div>

          {message ? <p className="text-center text-xs text-muted">{message}</p> : null}
        </div>
      </div>
    </div>
  );
}
