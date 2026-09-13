const DEFAULT_BASE = "http://127.0.0.1:18765";

let sidecarBase = DEFAULT_BASE;

export function setSidecarBase(url: string) {
  sidecarBase = url.replace(/\/$/, "");
}

export function getSidecarBase() {
  return sidecarBase;
}

export type DocumentItem = {
  id: string;
  title: string;
  source_path?: string | null;
  pages: number;
  block_count: number;
  review_score?: number | null;
  review_summary?: string | null;
  status: string;
  series?: string | null;
  era?: string | null;
  dpi?: number | null;
  error?: string | null;
  extra_metadata?: Record<string, unknown>;
  updated_at?: string | null;
  ocr_job?: {
    id: string;
    status: string;
    current_page: number;
    total_pages: number;
    error?: string | null;
  } | null;
};

export type SettingsData = {
  data_root: string;
  llm_provider?: string;
  dashscope_http_api_url: string;
  openai_base_url?: string;
  vision_model: string;
  chat_model: string;
  openai_vision_model?: string;
  openai_chat_model?: string;
  vision_pdf_dpi: number;
  vision_pdf_max_pages: number;
  vision_review_threshold: number;
  keep_page_images: boolean;
  ocr_page_concurrency: number;
  ocr_document_concurrency: number;
  ocr_api_concurrency: number;
  ocr_worker_processes?: number;
  has_api_key: boolean;
  has_openai_api_key?: boolean;
  delivery_submitter?: string;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${sidecarBase}${path}`, init);
  if (!res.ok) {
    const text = await res.text();
    let message = text || `HTTP ${res.status}`;
    try {
      const data = JSON.parse(text) as { detail?: unknown };
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) {
        message = data.detail
          .map((d) => (typeof d === "object" && d && "msg" in d ? String((d as { msg: string }).msg) : String(d)))
          .join("; ");
      }
    } catch {
      /* keep raw text */
    }
    throw new Error(message);
  }
  if (res.headers.get("content-type")?.includes("application/json")) {
    return res.json() as Promise<T>;
  }
  return res.text() as Promise<T>;
}

export async function healthCheck(): Promise<{ status: string; data_root: string }> {
  return api("/health");
}

export async function getSettings(): Promise<SettingsData> {
  return api("/settings");
}

export async function updateSettings(body: Record<string, unknown>): Promise<SettingsData> {
  return api("/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function listDocuments(params?: {
  q?: string;
  status?: string;
  series?: string;
  uncategorized?: boolean;
}): Promise<{ items: DocumentItem[]; total: number }> {
  const qs = new URLSearchParams();
  if (params?.q) qs.set("q", params.q);
  if (params?.status) qs.set("status", params.status);
  if (params?.series) qs.set("series", params.series);
  if (params?.uncategorized) qs.set("uncategorized", "true");
  const q = qs.toString();
  return api(`/library/documents${q ? `?${q}` : ""}`);
}

export async function updateDocument(
  id: string,
  body: {
    title?: string;
    series?: string;
    clear_series?: boolean;
    era?: string;
    delivery_metadata?: Record<string, unknown>;
  }
): Promise<DocumentItem> {
  return api(`/library/documents/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type FolderItem = { name: string; count: number };

export type FoldersResponse = {
  folders: FolderItem[];
  uncategorized_count: number;
  total: number;
};

export async function listFolders(): Promise<FoldersResponse> {
  return api("/library/folders");
}

export async function createFolder(name: string): Promise<{ name: string }> {
  return api("/library/folders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function renameFolder(name: string, newName: string): Promise<FoldersResponse> {
  return api(`/library/folders/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: newName }),
  });
}

export async function deleteFolder(name: string, move_to?: string): Promise<FoldersResponse> {
  const qs = move_to ? `?move_to=${encodeURIComponent(move_to)}` : "";
  return api(`/library/folders/${encodeURIComponent(name)}${qs}`, { method: "DELETE" });
}

export async function moveDocuments(
  document_ids: string[],
  folder: string | null
): Promise<FoldersResponse & { moved: number; folder: string | null }> {
  return api("/library/move-documents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_ids, folder }),
  });
}

export async function getDocument(id: string): Promise<DocumentItem> {
  return api(`/library/documents/${id}`);
}

export async function importPdf(body: {
  pdf_path: string;
  title?: string;
  series?: string;
  run_ocr?: boolean;
  dpi?: number;
}): Promise<{ document: DocumentItem; job?: { job_id: string } }> {
  return api("/library/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type ScanStartResult = {
  scan_job_id: string;
  status: string;
};

export type ScanDoneEvent = {
  type: "done" | "error" | "progress";
  current?: number;
  total?: number;
  file?: string;
  count?: number;
  skipped_sha_count?: number;
  queued_ocr_count?: number;
  error?: string;
};

export async function scanFolder(body?: {
  folder_path?: string;
  recursive?: boolean;
  run_ocr?: boolean;
  ocr_pending_in_library?: boolean;
  dpi?: number;
  max_pages?: number;
  auto_review?: boolean;
}): Promise<ScanStartResult> {
  return api("/library/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

export function subscribeScanEvents(jobId: string, onEvent: (data: ScanDoneEvent) => void) {
  const es = new EventSource(`${sidecarBase}/scan-jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    try {
      onEvent(JSON.parse(ev.data) as ScanDoneEvent);
    } catch {
      /* ignore */
    }
  };
  return es;
}

export type ReindexReport = {
  added: string[];
  updated: string[];
  skipped: string[];
  skipped_duplicate_sha: string[];
  invalid: string[];
  added_count: number;
  updated_count: number;
};

export async function reindexLibrary(): Promise<ReindexReport> {
  return api("/library/reindex", { method: "POST" });
}

export type MergeDocsReport = {
  copied: string[];
  skipped_same: string[];
  conflicts: string[];
  invalid: string[];
  copied_count: number;
  reindex?: ReindexReport;
};

export async function mergeExternalDocs(body: {
  source_path: string;
  run_reindex?: boolean;
}): Promise<MergeDocsReport> {
  return api("/library/merge-docs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteDocument(id: string): Promise<void> {
  await api(`/library/documents/${id}`, { method: "DELETE" });
}

export async function runOcr(
  id: string,
  body?: { dpi?: number; max_pages?: number; pages?: number[]; force?: boolean; auto_review?: boolean }
): Promise<{ job_id: string }> {
  return api(`/library/documents/${id}/ocr`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

export async function runReview(id: string): Promise<{ job_id: string; document_id: string }> {
  return api(`/library/documents/${id}/review`, { method: "POST" });
}

export async function batchOcr(body: {
  document_ids: string[];
  dpi?: number;
  max_pages?: number;
  auto_review?: boolean;
  force?: boolean;
}): Promise<{ jobs: Array<{ job_id: string; document_id: string }>; queued: string[]; skipped: string[]; count: number }> {
  return api("/library/ocr", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function batchReview(body: {
  document_ids: string[];
}): Promise<{ jobs: Array<{ job_id: string; document_id: string }>; queued: string[]; skipped: string[]; count: number }> {
  return api("/library/review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function stopOcr(
  id: string
): Promise<{ document_id: string; stopped: boolean; count: number }> {
  // Prefer the dedicated stop route; fall back to abandon-ocr for older sidecars
  // that were started before /ocr/stop was added.
  const tryStop = async () =>
    api<{ document_id: string; stopped: boolean }>(`/library/documents/${id}/ocr/stop`, {
      method: "POST",
    });

  try {
    const res = await tryStop();
    return { document_id: res.document_id, stopped: true, count: 1 };
  } catch (e) {
    const msg = String((e as Error).message || e);
    // FastAPI unknown-route: {"detail":"Not Found"}
    if (!/^not found$/i.test(msg.trim()) && !/\b404\b/.test(msg)) {
      throw e;
    }
  }

  const res = await api<{ abandoned: Array<{ document_id: string }>; count: number }>(
    "/library/abandon-ocr",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_ids: [id],
        reason: "OCR 已手动停止",
      }),
    }
  );
  if (!res.count) {
    throw new Error("OCR 未在运行或文档不存在");
  }
  return { document_id: id, stopped: true, count: res.count };
}

export async function rerunPages(
  id: string,
  pages: number[],
  body?: { dpi?: number; max_pages?: number; force?: boolean; auto_review?: boolean }
): Promise<{ job_id: string }> {
  return api(`/library/documents/${id}/rerun-pages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 「重跑页」默认强制覆盖当页已有结果
    body: JSON.stringify({ force: true, ...(body || {}), pages }),
  });
}

export async function getJob(jobId: string) {
  return api<{
    id: string;
    status: string;
    current_page: number;
    total_pages: number;
    error?: string;
  }>(`/jobs/${jobId}`);
}

export function docPdfUrl(id: string) {
  return `${sidecarBase}/library/documents/${id}/source.pdf`;
}

export function docThumbUrl(id: string) {
  return `${sidecarBase}/library/documents/${id}/thumb`;
}

export function docArtifactUrl(id: string, name: string) {
  return `${sidecarBase}/library/documents/${id}/artifacts/${name}`;
}

export function subscribeJobEvents(jobId: string, onEvent: (data: Record<string, unknown>) => void) {
  const es = new EventSource(`${sidecarBase}/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    try {
      onEvent(JSON.parse(ev.data));
    } catch {
      /* ignore */
    }
  };
  return es;
}

export type DeliveryPaper = {
  title: string;
  page_start: number;
  page_end: number;
  author?: string;
};

export type DeliveryJob = {
  id: string;
  status: "running" | "completed" | "failed";
  current: number;
  total: number;
  current_document_id?: string;
  results: Array<{
    document_id: string;
    ok: boolean;
    papers?: number;
    unmapped?: number;
    error?: string;
  }>;
  report_path?: string;
  error?: string;
};

export async function getDelivery(id: string): Promise<{
  document: DocumentItem;
  papers: DeliveryPaper[];
  entities?: Record<string, unknown> | null;
  ocr_validation: { errors: string[]; warnings: string[] };
}> {
  return api(`/library/documents/${id}/delivery`);
}

export async function saveDeliveryPapers(id: string, papers: DeliveryPaper[]) {
  return api<{ document_id: string; papers: DeliveryPaper[] }>(
    `/library/documents/${id}/papers`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ papers }),
    }
  );
}

export async function processWenshi(body: {
  document_ids: string[];
  split_papers?: boolean;
  extract_entities?: boolean;
  use_model?: boolean;
  export?: boolean;
  submitter?: string;
  output_root?: string;
}): Promise<{ job_id: string; total: number }> {
  return api("/library/process/wenshi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getDeliveryJob(jobId: string): Promise<DeliveryJob> {
  return api(`/delivery-jobs/${jobId}`);
}

export async function exportWenshi(body: {
  document_ids: string[];
  submitter?: string;
  output_root?: string;
}): Promise<{
  results: Array<{
    document_id: string;
    ocr_path?: string;
    entity_path?: string | null;
    errors: string[];
    warnings?: string[];
  }>;
  success_count: number;
  failure_count: number;
}> {
  return api("/library/export/wenshi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
