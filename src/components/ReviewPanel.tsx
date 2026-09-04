import { cn } from "@/lib/utils";

export type ReviewDoc = {
  score?: number;
  summary?: string;
  issues?: string[];
  needs_rerun?: boolean;
  threshold?: number;
  model?: string;
};

type Props = {
  review: ReviewDoc | null;
  className?: string;
};

export function ReviewPanel({ review, className }: Props) {
  if (!review) {
    return (
      <div className={cn("p-4 text-sm text-muted", className)}>
        暂无质检结果。完整 OCR 结束后会由模型自动生成 review。
      </div>
    );
  }

  const score = review.score;
  const low = score != null && review.threshold != null && score < review.threshold;
  const issues = review.issues?.filter(Boolean) ?? [];

  return (
    <div className={cn("space-y-4 overflow-auto p-4 text-sm", className)}>
      <div className="flex flex-wrap items-center gap-2">
        {score != null ? (
          <span
            className={cn(
              "rounded-lg border px-2.5 py-1 text-sm font-semibold tabular-nums",
              low || review.needs_rerun
                ? "border-amber-300 bg-amber-50 text-amber-900"
                : "border-emerald-200 bg-emerald-50 text-emerald-800"
            )}
          >
            得分 {score.toFixed(2)}
            {review.threshold != null ? ` / 阈值 ${review.threshold}` : ""}
          </span>
        ) : null}
        {review.needs_rerun ? (
          <span className="rounded border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs text-amber-900">
            建议重跑
          </span>
        ) : (
          <span className="rounded border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-xs text-emerald-800">
            质检通过
          </span>
        )}
        {review.model ? <span className="text-xs text-muted">模型 {review.model}</span> : null}
      </div>

      {review.summary ? (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">结论</h3>
          <p className="leading-6 text-ink">{review.summary}</p>
        </div>
      ) : null}

      {issues.length > 0 ? (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">问题列表</h3>
          <ul className="list-disc space-y-1.5 pl-5 text-ink">
            {issues.map((issue, i) => (
              <li key={i} className="leading-6 break-words">
                {issue}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-xs text-muted">模型未列出具体问题。</p>
      )}
    </div>
  );
}
