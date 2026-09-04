/** Detect / repair degenerate OCR bboxes. */

const PAGE = 1000;
const FULL_AREA = PAGE * PAGE;
const DEGENERATE_AREA_FRAC = 0.85;

export function bboxArea(bbox: number[] | undefined | null): number {
  if (!Array.isArray(bbox) || bbox.length < 4) return 0;
  const [x0, y0, x1, y1] = bbox.map(Number);
  return Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
}

export function isDegenerateBbox(bbox: number[] | undefined | null): boolean {
  if (!Array.isArray(bbox) || bbox.length < 4) return true;
  return bboxArea(bbox) >= DEGENERATE_AREA_FRAC * FULL_AREA;
}

export type LayoutBlockLike = {
  type: string;
  text: string;
  bbox: number[];
  bboxEstimated?: boolean;
};

export function repairPageBlocks<T extends LayoutBlockLike>(blocks: T[]): T[] {
  if (!blocks.length) return blocks;
  const degenerateCount = blocks.filter((b) => isDegenerateBbox(b.bbox)).length;
  if (degenerateCount < Math.max(1, Math.ceil(blocks.length * 0.6))) {
    return blocks;
  }

  const marginX = 70;
  const marginY = 55;
  const gap = 10;
  const usableH = PAGE - marginY * 2 - gap * Math.max(0, blocks.length - 1);

  const weights = blocks.map((b) => {
    const lines = Math.max(1, (b.text.match(/\n/g) || []).length + 1);
    const chars = Math.max(8, b.text.replace(/\s+/g, "").length);
    return Math.max(1, lines * 1.2 + chars / 36);
  });
  const sum = weights.reduce((a, w) => a + w, 0) || 1;

  let y = marginY;
  return blocks.map((b, i) => {
    const h = Math.max(22, (weights[i] / sum) * usableH);
    const y1 = Math.min(PAGE - marginY, y + h);
    const bbox = [marginX, y, PAGE - marginX, y1];
    y = y1 + gap;
    return { ...b, bbox, bboxEstimated: true };
  });
}

export function pageHasEstimatedBboxes(blocks: { bboxEstimated?: boolean }[]): boolean {
  return blocks.some((b) => b.bboxEstimated);
}
