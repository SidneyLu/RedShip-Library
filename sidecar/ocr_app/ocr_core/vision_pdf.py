"""Scan-PDF → page images → VL layout JSON → Markdown + layout."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

LAYOUT_SCHEMA_VERSION = 1
_SKIP_RAG_TYPES = {"pagefooter", "pageheader", "footer", "header"}


def is_nearly_blank_image(
    path: Path,
    *,
    white_ratio_thresh: float = 0.992,
    std_thresh: float = 6.0,
) -> bool:
    """True when a page image is blank/near-blank (common trailing empty PDF pages).

    Thinking VL models often hang or generate forever on empty pages; callers should
    skip the VL call and treat the page as empty.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        return False
    try:
        pix = fitz.Pixmap(str(path))
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)  # drop alpha
        if pix.n > 1:
            pix = fitz.Pixmap(fitz.csGRAY, pix)
        samples = pix.samples
        n = len(samples)
        if n == 0:
            return True
        # Subsample for speed (~640px long-edge equivalent).
        stride = max(1, max(pix.width, pix.height) // 640)
        total = 0
        total_sq = 0
        white = 0
        count = 0
        for i in range(0, n, stride):
            v = samples[i]
            total += v
            total_sq += v * v
            if v >= 248:
                white += 1
            count += 1
        mean = total / count
        var = (total_sq / count) - (mean * mean)
        std = var ** 0.5 if var > 0 else 0.0
        if std < std_thresh:
            return True
        return (white / count) >= white_ratio_thresh
    except Exception as exc:
        logger.warning("blank-detect failed for {}: {}", path, exc)
        return False


@dataclass
class LayoutBlock:
    type: str
    text: str
    bbox: list[float]
    page: int


@dataclass
class VisionPdfResult:
    layout: dict[str, Any]
    markdown: str
    pages: int
    block_count: int
    empty_pages: int
    vision_model: str


def _normalize_bbox(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        vals = [float(raw[i]) for i in range(4)]
    except (TypeError, ValueError):
        return None
    if all(0.0 <= v <= 1.5 for v in vals) and max(vals) <= 1.5:
        vals = [v * 1000.0 for v in vals]
    return [max(0.0, min(1000.0, v)) for v in vals]


def _bbox_area(bbox: list[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _is_degenerate_bbox(bbox: list[float]) -> bool:
    if len(bbox) < 4:
        return True
    return _bbox_area(bbox) >= 0.85 * 1000.0 * 1000.0 or _bbox_area(bbox) < 1.0


def _repair_page_blocks(blocks: list[LayoutBlock], page: int) -> list[LayoutBlock]:
    if not blocks:
        return blocks
    bad = sum(1 for b in blocks if _is_degenerate_bbox(b.bbox))
    if bad < max(1, (len(blocks) + 1) // 2):
        return blocks

    logger.warning(
        "Page {}: {}/{} blocks have degenerate bbox; estimating vertical stack",
        page,
        bad,
        len(blocks),
    )
    margin_x, margin_y, gap = 70.0, 55.0, 10.0
    usable = 1000.0 - margin_y * 2 - gap * max(0, len(blocks) - 1)
    weights: list[float] = []
    for b in blocks:
        lines = max(1, b.text.count("\n") + 1)
        chars = max(8, len("".join(b.text.split())))
        weights.append(max(1.0, lines * 1.2 + chars / 36.0))
    total_w = sum(weights) or 1.0
    y = margin_y
    fixed: list[LayoutBlock] = []
    for b, w in zip(blocks, weights):
        h = max(22.0, (w / total_w) * usable)
        y1 = min(1000.0 - margin_y, y + h)
        fixed.append(
            LayoutBlock(
                type=b.type,
                text=b.text,
                bbox=[margin_x, y, 1000.0 - margin_x, y1],
                page=b.page,
            )
        )
        y = y1 + gap
    return fixed


def _parse_blocks_payload(payload: Any, page: int) -> list[LayoutBlock]:
    blocks_raw: list[Any] = []
    if isinstance(payload, dict):
        blocks_raw = payload.get("blocks") or payload.get("items") or []
    elif isinstance(payload, list):
        blocks_raw = payload
    out: list[LayoutBlock] = []
    for item in blocks_raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        btype = str(item.get("type") or "text").strip().lower() or "text"
        bbox = _normalize_bbox(item.get("bbox") or item.get("box") or item.get("rect"))
        if not bbox:
            bbox = [0.0, 0.0, 0.0, 0.0]
        out.append(LayoutBlock(type=btype, text=text, bbox=bbox, page=page))
    return out


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse_layout_json_text(text: str, page: int, *, repair: bool = True) -> list[LayoutBlock]:
    raw = (text or "").strip()
    if not raw:
        return []
    m = _JSON_FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("VL layout JSON parse failed on page {}", page)
                return []
        else:
            return []
    blocks = _parse_blocks_payload(payload, page)
    return _repair_page_blocks(blocks, page) if repair else blocks


def render_pdf_page(pdf_path: Path, page_num: int, out_path: Path, *, dpi: int) -> Path:
    """Render a single 1-based PDF page to PNG."""
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError("PyMuPDF required. pip install pymupdf") from e

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        if page_num < 1 or page_num > len(doc):
            raise ValueError(f"page {page_num} out of range (1-{len(doc)})")
        zoom = dpi / 72.0
        pix = doc.load_page(page_num - 1).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(str(out_path))
        return out_path
    finally:
        doc.close()


def render_pdf_pages(pdf_path: Path, out_dir: Path, *, dpi: int, max_pages: int) -> list[Path]:
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError("PyMuPDF required. pip install pymupdf") from e

    doc = fitz.open(pdf_path)
    try:
        n = min(len(doc), max_pages)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        paths: list[Path] = []
        for i in range(n):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out = out_dir / f"page-{i + 1:04d}.png"
            pix.save(str(out))
            paths.append(out)
        return paths
    finally:
        doc.close()


def render_thumbnail(pdf_path: Path, out_path: Path, *, dpi: int = 72) -> None:
    try:
        import fitz
    except ImportError:
        return
    doc = fitz.open(pdf_path)
    try:
        if len(doc) == 0:
            return
        zoom = dpi / 72.0
        pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_path))
    finally:
        doc.close()


def pdf_page_count(pdf_path: Path) -> int:
    import fitz

    doc = fitz.open(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


def blocks_to_markdown(blocks: list[LayoutBlock], *, title: str) -> str:
    lines: list[str] = [f"# {title}", ""]
    current_page: int | None = None
    for b in blocks:
        if b.type in _SKIP_RAG_TYPES:
            continue
        if current_page != b.page:
            current_page = b.page
            lines.append(f"<!-- page: {b.page} -->")
            lines.append("")
        if b.type in {"sectionheader", "title"}:
            lines.append(f"## {b.text}")
            lines.append("")
        else:
            lines.append(b.text)
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def blocks_to_layout(blocks: list[LayoutBlock], *, pages: int) -> dict[str, Any]:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(
            {"type": b.type, "text": b.text, "bbox": b.bbox}
        )
    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "norm": 1000,
        "pages": [
            {
                "page": p,
                "width_norm": 1000,
                "height_norm": 1000,
                "blocks": by_page.get(p, []),
            }
            for p in range(1, pages + 1)
        ],
    }


def merge_page_blocks(
    layout: dict[str, Any],
    page: int,
    new_blocks: list[LayoutBlock],
    *,
    force: bool = False,
) -> dict[str, Any]:
    pages = layout.get("pages") or []
    entry = {
        "page": page,
        "width_norm": 1000,
        "height_norm": 1000,
        "blocks": [{"type": b.type, "text": b.text, "bbox": b.bbox} for b in new_blocks],
    }
    replaced = False
    out_pages: list[dict[str, Any]] = []
    for p in pages:
        if int(p.get("page", 0)) == page:
            # Keep already-recognized pages unless explicitly forced.
            if not force and page_has_real_content(p.get("blocks") or []):
                out_pages.append(p)
            else:
                out_pages.append(entry)
            replaced = True
        else:
            out_pages.append(p)
    if not replaced:
        out_pages.append(entry)
        out_pages.sort(key=lambda x: int(x.get("page", 0)))
    layout = dict(layout)
    layout["pages"] = out_pages
    return layout


def layout_blocks_from_dict(layout: dict[str, Any]) -> list[LayoutBlock]:
    blocks: list[LayoutBlock] = []
    for page in layout.get("pages") or []:
        pnum = int(page.get("page", 1))
        for item in page.get("blocks") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            bbox = item.get("bbox") or [0, 0, 0, 0]
            blocks.append(
                LayoutBlock(
                    type=str(item.get("type") or "text"),
                    text=text,
                    bbox=[float(x) for x in bbox[:4]],
                    page=pnum,
                )
            )
    return blocks


def load_layout_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


PLACEHOLDER_TEXT_MARKERS = (
    "【本页因网络中断未能识别",
    "【本页因云端内容安全审核未能识别",
    "【本页模型返回空结果未能识别",
)


def is_placeholder_text(text: str) -> bool:
    """True for empty text or known skip/placeholder OCR notes."""
    t = (text or "").strip()
    if not t:
        return True
    return any(marker in t for marker in PLACEHOLDER_TEXT_MARKERS)


def page_has_real_content(blocks: list[Any] | None) -> bool:
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        text = str(b.get("text") or "").strip()
        if text and not is_placeholder_text(text):
            return True
    return False


def pages_with_content(layout: dict[str, Any]) -> set[int]:
    """Pages that already have real OCR text (placeholder-only pages are excluded)."""
    out: set[int] = set()
    for page in layout.get("pages") or []:
        pnum = int(page.get("page", 0))
        if pnum < 1:
            continue
        if page_has_real_content(page.get("blocks") or []):
            out.add(pnum)
    return out


def pages_to_process(
    target_pages: list[int],
    layout: dict[str, Any],
    *,
    explicit_pages: list[int] | None,
    force: bool = False,
) -> list[int]:
    """Return page numbers to OCR.

    By default skips pages that already have real content (placeholder-only pages
    still count as empty). Explicit page lists are filtered the same way unless
    force=True.
    """
    if explicit_pages is not None:
        candidates = sorted({int(p) for p in explicit_pages if int(p) >= 1})
    else:
        candidates = list(target_pages)
    if force:
        return candidates
    done = pages_with_content(layout)
    return [p for p in candidates if p not in done]


def write_artifacts(
    out_dir: Path,
    *,
    markdown: str,
    layout: dict[str, Any],
    review: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "content.md"
    layout_path = out_dir / "layout.json"
    md_path.write_text(markdown, encoding="utf-8")
    layout_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    review_path: Path | None = None
    if review is not None:
        review_path = out_dir / "review.json"
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, layout_path, review_path
