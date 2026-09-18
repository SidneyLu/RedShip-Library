import { cn } from "@/lib/utils";

export type OcrProgressState = {
  current: number;
  total: number;
  label?: string;
  error?: string | null;
};

type Props = {
  progress: OcrProgressState | null;
  className?: string;
  compact?: boolean;
};

export function OcrProgressBar({ progress, className, compact }: Props) {
  if (!progress) return null;
  const total = Math.max(0, progress.total);
  const current = Math.max(0, progress.current);
  const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
  const failed = Boolean(progress.error);

  return (
    <div className={cn("w-full", className)} role="status" aria-live="polite">
      <div className={cn("mb-1 flex items-center justify-between gap-2", compact ? "text-[10px]" : "text-xs")}>
        <span className={cn(failed ? "text-crimson-700" : "text-muted")}>
          {failed
            ? `OCR 失败：${progress.error}`
            : progress.label || (total > 0 ? `识别中 ${current}/${total} 页` : "OCR 准备中…")}
        </span>
        {!failed && total > 0 ? <span className="tabular-nums text-muted">{pct}%</span> : null}
      </div>
      <div className={cn("overflow-hidden rounded-full bg-canvas", compact ? "h-1.5" : "h-2")}>
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-300 ease-out",
            failed ? "bg-crimson-600" : "bg-crimson-700"
          )}
          style={{ width: failed ? "100%" : `${total > 0 ? Math.max(pct, current > 0 ? 4 : 2) : 8}%` }}
        />
      </div>
    </div>
  );
}
