export type OcrBlockColor = {
  fill: string;
  border: string;
  bar: string;
  label: string;
};

const COLORS: Record<string, OcrBlockColor> = {
  text: {
    fill: "rgba(59, 130, 246, 0.22)",
    border: "rgba(37, 99, 235, 0.85)",
    bar: "#3b82f6",
    label: "text-blue-700",
  },
  sectionheader: {
    fill: "rgba(244, 63, 94, 0.22)",
    border: "rgba(225, 29, 72, 0.9)",
    bar: "#e11d48",
    label: "text-rose-700",
  },
  title: {
    fill: "rgba(244, 63, 94, 0.22)",
    border: "rgba(225, 29, 72, 0.9)",
    bar: "#e11d48",
    label: "text-rose-700",
  },
  pagefooter: {
    fill: "rgba(139, 92, 246, 0.2)",
    border: "rgba(124, 58, 237, 0.85)",
    bar: "#8b5cf6",
    label: "text-violet-700",
  },
  pageheader: {
    fill: "rgba(139, 92, 246, 0.2)",
    border: "rgba(124, 58, 237, 0.85)",
    bar: "#8b5cf6",
    label: "text-violet-700",
  },
  caption: {
    fill: "rgba(245, 158, 11, 0.2)",
    border: "rgba(217, 119, 6, 0.85)",
    bar: "#f59e0b",
    label: "text-amber-700",
  },
};

const DEFAULT_COLOR: OcrBlockColor = {
  fill: "rgba(100, 116, 139, 0.2)",
  border: "rgba(71, 85, 105, 0.85)",
  bar: "#64748b",
  label: "text-slate-600",
};

export function ocrBlockColor(type: string | undefined | null): OcrBlockColor {
  const key = String(type || "text").trim().toLowerCase();
  return COLORS[key] || DEFAULT_COLOR;
}
