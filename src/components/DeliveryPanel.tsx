import { useCallback, useEffect, useState } from "react";
import {
  getDelivery,
  getDeliveryJob,
  processWenshi,
  saveDeliveryPapers,
  updateDocument,
  type DeliveryPaper,
  type DocumentItem,
} from "@/lib/api";

type Props = {
  documentId: string;
  document: DocumentItem;
  onGoToPage: (page: number) => void;
  onDocumentChange: (document: DocumentItem) => void;
};

export function DeliveryPanel({
  documentId,
  document,
  onGoToPage,
  onDocumentChange,
}: Props) {
  const [papers, setPapers] = useState<DeliveryPaper[]>([]);
  const [validation, setValidation] = useState<{ errors: string[]; warnings: string[] }>({
    errors: [],
    warnings: [],
  });
  const [metadata, setMetadata] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    const data = await getDelivery(documentId);
    setPapers(data.papers || []);
    setValidation(data.ocr_validation);
    setMetadata(data.document.extra_metadata || {});
  }, [documentId]);

  useEffect(() => {
    load().catch((error) => setMessage(String(error)));
  }, [load]);

  const setPaper = (index: number, patch: Partial<DeliveryPaper>) => {
    setPapers((current) =>
      current.map((paper, paperIndex) =>
        paperIndex === index ? { ...paper, ...patch } : paper
      )
    );
  };

  const saveMetadata = async () => {
    setBusy(true);
    try {
      const next = await updateDocument(documentId, { delivery_metadata: metadata });
      onDocumentChange(next);
      setMessage("交付元数据已保存");
    } catch (error) {
      setMessage(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  };

  const savePapers = async () => {
    setBusy(true);
    try {
      const result = await saveDeliveryPapers(documentId, papers);
      setPapers(result.papers);
      setMessage("篇目页范围已保存");
      await load();
    } catch (error) {
      setMessage(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  };

  const generate = async () => {
    setBusy(true);
    setMessage("正在识别篇目并提取实体…");
    try {
      const started = await processWenshi({
        document_ids: [documentId],
        split_papers: true,
        extract_entities: true,
        use_model: true,
        export: false,
      });
      while (true) {
        const job = await getDeliveryJob(started.job_id);
        if (job.status !== "running") {
          const result = job.results[0];
          if (!result?.ok) throw new Error(result?.error || job.error || "处理失败");
          setMessage(`处理完成：${result.papers ?? 0} 篇，未映射 ${result.unmapped ?? 0} 项`);
          await load();
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    } catch (error) {
      setMessage(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  };

  const field = (
    key: string,
    label: string,
    type: "text" | "number" = "text"
  ) => (
    <label className="block">
      <span className="text-xs text-muted">{label}</span>
      <input
        type={type}
        className="mt-1 w-full rounded border border-border px-2 py-1.5 text-sm"
        value={String(metadata[key] ?? "")}
        onChange={(event) =>
          setMetadata((current) => ({
            ...current,
            [key]:
              type === "number"
                ? event.target.value
                  ? Number(event.target.value)
                  : null
                : event.target.value,
          }))
        }
      />
    </label>
  );

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-auto p-4 text-sm">
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-semibold text-crimson-800">交付元数据</h2>
          <button
            type="button"
            disabled={busy}
            onClick={saveMetadata}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-crimson-50 disabled:opacity-50"
          >
            保存元数据
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {field("original_filename", "源 PDF 完整文件名")}
          {field("district", "地区")}
          {field("volume", "辑次", "number")}
          {field("pub_year", "出版年份", "number")}
          {field("pub_org", "出版单位")}
          {field("delivery_notes", "备注")}
        </div>
        <label className="mt-2 flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={Boolean(metadata.proofread)}
            onChange={(event) =>
              setMetadata((current) => ({ ...current, proofread: event.target.checked }))
            }
          />
          已人工校对
        </label>
      </section>

      <section>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold text-crimson-800">篇目与物理页范围</h2>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={generate}
              className="rounded bg-crimson-700 px-2 py-1 text-xs text-white disabled:opacity-50"
            >
              AI 生成篇目并提取实体
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={savePapers}
              className="rounded border border-border px-2 py-1 text-xs disabled:opacity-50"
            >
              保存篇目
            </button>
          </div>
        </div>
        <div className="space-y-2">
          {papers.map((paper, index) => (
            <div key={`${index}-${paper.page_start}`} className="grid grid-cols-[1fr_4rem_4rem_auto] gap-1">
              <input
                className="min-w-0 rounded border border-border px-2 py-1"
                value={paper.title}
                onChange={(event) => setPaper(index, { title: event.target.value })}
              />
              <button
                type="button"
                title="跳到起始页"
                onClick={() => onGoToPage(paper.page_start)}
                className="rounded border border-border"
              >
                {paper.page_start}
              </button>
              <input
                type="number"
                min={paper.page_start}
                max={document.pages}
                className="rounded border border-border px-1 text-center"
                value={paper.page_end}
                onChange={(event) => setPaper(index, { page_end: Number(event.target.value) })}
              />
              <button
                type="button"
                onClick={() => setPapers((current) => current.filter((_, i) => i !== index))}
                className="px-1 text-xs text-muted hover:text-crimson-700"
              >
                删除
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() =>
              setPapers((current) => [
                ...current,
                { title: "新篇目", page_start: 1, page_end: document.pages || 1 },
              ])
            }
            className="text-xs text-crimson-700"
          >
            + 添加篇目
          </button>
        </div>
      </section>

      <section className="rounded border border-border bg-canvas/50 p-3 text-xs">
        <p className="font-medium">甲方格式校验</p>
        {validation.errors.length ? (
          <p className="mt-1 text-crimson-700">错误：{validation.errors.join("；")}</p>
        ) : (
          <p className="mt-1 text-emerald-700">OCR JSON 硬性规则通过</p>
        )}
        {validation.warnings.length ? (
          <p className="mt-1 text-amber-800">提醒：{validation.warnings.join("；")}</p>
        ) : null}
      </section>
      {message ? <p className="text-xs text-muted">{message}</p> : null}
    </div>
  );
}
