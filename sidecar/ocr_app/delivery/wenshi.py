"""Build, enrich, validate, and export the client's Wenshi JSON format."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from ocr_app.config import active_chat_model, settings
from ocr_app.db.models import Document
from ocr_app.library.paths import doc_dir, parse_metadata
from ocr_app.ocr_core.vl_client import get_vl_client

ENTITY_TYPES = ("TIME", "PLACE", "PERSON", "EVENT")
SKIP_BLOCK_TYPES = {"pagefooter", "pageheader", "footer", "header"}
GENERIC_HEADINGS = {
    "目录", "目次", "前言", "序", "序言", "后记", "编后记", "版权页",
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _page_text(page: dict[str, Any]) -> str:
    texts: list[str] = []
    for block in page.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").lower() in SKIP_BLOCK_TYPES:
            continue
        text = str(block.get("text") or "").strip()
        if text:
            # The client validator reserves corner brackets for template placeholders.
            texts.append(text.replace("【", "[").replace("】", "]"))
    return "\n".join(texts)


def layout_to_pages(layout: dict[str, Any], page_count: int) -> list[dict[str, Any]]:
    """Return a complete 1..N physical-page sequence, including empty pages."""
    entries: dict[int, dict[str, Any]] = {}
    for raw in layout.get("pages") or []:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get("page"))
        except (TypeError, ValueError):
            continue
        if number >= 1:
            entries[number] = raw
    total = max(int(page_count or 0), max(entries, default=0))
    return [
        {"page_no": number, "content": _page_text(entries.get(number, {}))}
        for number in range(1, total + 1)
    ]


def _original_filename(doc: Document, meta: dict[str, Any]) -> str:
    name = str(meta.get("original_filename") or "").strip()
    if not name and doc.source_path:
        name = Path(doc.source_path).name
    if not name or name.lower() == "source.pdf":
        name = f"{doc.title}.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def delivery_metadata(doc: Document) -> dict[str, Any]:
    meta = parse_metadata(doc.extra_metadata)
    return {
        "original_filename": _original_filename(doc, meta),
        "district": str(meta.get("district") or doc.series or ""),
        "volume": meta.get("volume"),
        "pub_year": meta.get("pub_year"),
        "pub_org": str(meta.get("pub_org") or ""),
        "proofread": bool(meta.get("proofread", False)),
        "notes": str(meta.get("delivery_notes") or ""),
    }


def build_ocr_payload(doc: Document, *, submitter: str | None = None) -> dict[str, Any]:
    ddir = doc_dir(doc.id)
    layout = _read_json(ddir / "layout.json", {})
    if not isinstance(layout, dict) or not layout.get("pages"):
        raise ValueError("layout.json not found or empty")
    pages = layout_to_pages(layout, doc.pages)
    if not pages:
        raise ValueError("document has no pages")
    meta = delivery_metadata(doc)
    papers = _read_json(ddir / "papers.json", [])
    if not isinstance(papers, list):
        papers = []
    payload: dict[str, Any] = {
        "filename": meta["original_filename"],
        "title": doc.title,
        "district": meta["district"],
        "volume": meta["volume"],
        "pub_year": meta["pub_year"],
        "pub_org": meta["pub_org"],
        "page_count": len(pages),
        "pages": pages,
        "papers": papers,
        "meta": {
            "ocr_tool": doc.vision_model or "OCR Library",
            "proofread": meta["proofread"],
            "submitter": (submitter or settings.delivery_submitter or "").strip(),
            "finished_at": date.today().isoformat(),
            "notes": meta["notes"],
        },
    }
    return payload


def _heading_candidates(layout: dict[str, Any], page_count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for page in layout.get("pages") or []:
        if not isinstance(page, dict):
            continue
        try:
            page_no = int(page.get("page"))
        except (TypeError, ValueError):
            continue
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").lower() not in {"title", "sectionheader"}:
                continue
            title = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
            if (
                not title
                or title in GENERIC_HEADINGS
                or len(title) < 2
                or len(title) > 120
                or page_no < 1
                or page_no > page_count
            ):
                continue
            key = (title, page_no)
            if key not in seen:
                seen.add(key)
                candidates.append({"title": title, "page_start": page_no})
    candidates.sort(key=lambda item: (item["page_start"], item["title"]))
    return candidates


def _finalize_papers(items: Iterable[dict[str, Any]], page_count: int) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (
            re.sub(r"\s+", " ", str(item.get("title") or ""))
            .strip()
            .replace("【", "[")
            .replace("】", "]")
        )
        try:
            start = int(item.get("page_start"))
        except (TypeError, ValueError):
            continue
        if not title or start < 1 or start > page_count:
            continue
        key = (title, start)
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = {"title": title, "page_start": start}
        try:
            row["_requested_end"] = int(item.get("page_end"))
        except (TypeError, ValueError):
            row["_requested_end"] = None
        author = str(item.get("author") or "").strip()
        if author:
            row["author"] = author
        cleaned.append(row)
    cleaned.sort(key=lambda item: item["page_start"])
    for index, row in enumerate(cleaned):
        next_start = cleaned[index + 1]["page_start"] if index + 1 < len(cleaned) else page_count + 1
        requested_end = row.pop("_requested_end", None)
        row["page_end"] = min(
            page_count,
            max(row["page_start"], requested_end or (next_start - 1)),
        )
    return cleaned


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        value = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


async def split_papers(doc: Document, *, use_model: bool = True) -> list[dict[str, Any]]:
    ddir = doc_dir(doc.id)
    layout = _read_json(ddir / "layout.json", {})
    pages = layout_to_pages(layout, doc.pages)
    candidates = _heading_candidates(layout, len(pages))
    proposed: list[dict[str, Any]] = candidates
    if use_model and candidates:
        compact = json.dumps(candidates[:500], ensure_ascii=False)
        prompt = (
            "你是文史资料目录整理器。下面是OCR版面检测到的标题候选（含PDF物理页号）。"
            "删除目录项、页眉、重复项和非篇目标题，保留真正文章；不要改写标题。"
            "只输出JSON数组，每项含title、page_start，可含author；不要输出解释。\n"
            + compact
        )
        try:
            response = await get_vl_client().chat(
                [{"role": "user", "content": prompt}],
                model=active_chat_model(),
                temperature=0,
            )
            content = str(response["choices"][0]["message"]["content"])
            model_items = _extract_json_array(content)
            if model_items:
                proposed = model_items
        except Exception:
            proposed = candidates
    papers = _finalize_papers(proposed, len(pages))
    _write_json(ddir / "papers.json", papers)
    return papers


def save_papers(document_id: str, papers: list[dict[str, Any]], page_count: int) -> list[dict[str, Any]]:
    cleaned = _finalize_papers(papers, page_count)
    _write_json(doc_dir(document_id) / "papers.json", cleaned)
    return cleaned


def _empty_entities() -> dict[str, list[dict[str, Any]]]:
    return {kind: [] for kind in ENTITY_TYPES}


def _normalize_time(surface: str) -> str:
    match = re.search(r"((?:19|20)\d{2})年(?:([01]?\d)月(?:([0-3]?\d)日)?)?", surface)
    if match:
        parts = [match.group(1)]
        if match.group(2):
            parts.append(match.group(2).zfill(2))
        if match.group(3):
            parts.append(match.group(3).zfill(2))
        return "-".join(parts)
    digits = str.maketrans("〇零一二三四五六七八九", "00123456789")
    match = re.search(r"([〇零一二三四五六七八九]{4})年", surface)
    if match:
        return match.group(1).translate(digits)
    match = re.search(r"民国([一二三四五六七八九十百〇零\d]+)年", surface)
    if match:
        raw = match.group(1)
        values = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if len(raw) == 2 and all(char in values for char in raw):
            return f"{1911 + values[raw[0]]}~{1911 + values[raw[1]]}"
        try:
            year = int(raw)
        except ValueError:
            if "十" in raw:
                left, _, right = raw.partition("十")
                year = values.get(left, 1) * 10 + values.get(right, 0)
            else:
                year = values.get(raw, 0)
        return str(1911 + year) if year > 0 else ""
    return ""


def _normalized_text_map(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char.isspace():
            pending_space = bool(normalized)
            continue
        if pending_space:
            normalized.append(" ")
            offsets.append(index)
            pending_space = False
        normalized.append(char)
        offsets.append(index)
    return "".join(normalized), offsets


def _occurrences(text: str, surface: str) -> list[tuple[int, int]]:
    if not surface:
        return []
    exact = [(m.start(), m.end()) for m in re.finditer(re.escape(surface), text)]
    if exact:
        return exact
    compact_surface = re.sub(r"\s+", "", surface)
    if compact_surface:
        flexible = r"\s*".join(re.escape(char) for char in compact_surface)
        flexible_hits = [(m.start(), m.end()) for m in re.finditer(flexible, text)]
        if flexible_hits:
            return flexible_hits
    normalized_text, offsets = _normalized_text_map(text)
    normalized_surface, _ = _normalized_text_map(surface)
    found: list[tuple[int, int]] = []
    for match in re.finditer(re.escape(normalized_surface), normalized_text):
        if match.start() < len(offsets) and match.end() - 1 < len(offsets):
            found.append((offsets[match.start()], offsets[match.end() - 1] + 1))
    return found


def _map_entities(
    raw_items: Iterable[dict[str, Any]],
    text: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    grouped = _empty_entities()
    unmapped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in raw_items:
        kind = str(raw.get("type") or "").upper()
        surface = str(raw.get("surface") or "").strip()
        if kind not in ENTITY_TYPES or not surface:
            continue
        hits = _occurrences(text, surface)
        if not hits:
            unmapped.append({"type": kind, "surface": surface})
            continue
        for start, end in hits:
            mapped_surface = text[start:end]
            key = (kind, mapped_surface, start)
            if key in seen:
                continue
            seen.add(key)
            item: dict[str, Any] = {
                "surface": mapped_surface,
                "context": text[max(0, start - 50) : min(len(text), end + 50)],
            }
            recommended = {"TIME": "normalized", "PLACE": "normalized", "PERSON": "role", "EVENT": "label"}[kind]
            item[recommended] = str(
                raw.get(recommended)
                or (_normalize_time(mapped_surface) if kind == "TIME" else "")
            )
            if kind == "PLACE":
                item["district"] = str(raw.get("district") or "")
            grouped[kind].append(item)
    return grouped, unmapped


def _entity_prompt(text: str) -> str:
    return (
        "从下面OCR原文提取所有明确出现的实体。只输出JSON数组。"
        "每项必须含type(TIME/PLACE/PERSON/EVENT之一)和surface；surface必须逐字照抄原文。"
        "TIME可加normalized，PLACE可加normalized和district，PERSON可加role，EVENT可加label。"
        "不要输出context，不要用背景知识补充，不要解释。\n原文：\n" + text
    )


async def _extract_for_text(text: str, *, use_model: bool) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    raw_items: list[dict[str, Any]] = []
    if use_model and text.strip():
        # Keep requests bounded; overlap reduces missed entities at chunk boundaries.
        step, size = 5000, 6000
        for start in range(0, len(text), step):
            chunk = text[start : start + size]
            try:
                response = await get_vl_client().chat(
                    [{"role": "user", "content": _entity_prompt(chunk)}],
                    model=active_chat_model(),
                    temperature=0,
                )
                raw_items.extend(
                    _extract_json_array(str(response["choices"][0]["message"]["content"]))
                )
            except Exception:
                continue
    # Deterministic TIME fallback also makes no-model/offline processing useful.
    for match in re.finditer(
        r"(?:民国[一二三四五六七八九十百〇零\d]{1,5}年|"
        r"(?:一|二|三|四|五|六|七|八|九|〇|零){4}年|"
        r"(?:19|20)\d{2}年(?:\d{1,2}月(?:\d{1,2}日)?)?)",
        text,
    ):
        raw_items.append({"type": "TIME", "surface": match.group(0), "normalized": ""})
    return _map_entities(raw_items, text)


async def extract_entities(doc: Document, *, use_model: bool = True) -> dict[str, Any]:
    ocr = build_ocr_payload(doc)
    pages = {int(page["page_no"]): str(page["content"]) for page in ocr["pages"]}
    papers = _read_json(doc_dir(doc.id) / "papers.json", [])
    output: dict[str, Any] = {
        "filename": ocr["filename"],
        "title": ocr["title"],
        "papers": [],
        "entities": _empty_entities(),
        "meta": {
            "extractor": settings.delivery_submitter,
            "model_or_tool": active_chat_model(),
            "finished_at": date.today().isoformat(),
            "notes": "",
        },
        "_unmapped": [],
    }
    if papers:
        for paper in papers:
            start, end = int(paper["page_start"]), int(paper["page_end"])
            text = "\n".join(pages.get(number, "") for number in range(start, end + 1))
            grouped, unmapped = await _extract_for_text(text, use_model=use_model)
            output["papers"].append({"title": paper["title"], "entities": grouped})
            output["_unmapped"].extend({"paper": paper["title"], **item} for item in unmapped)
    else:
        text = "\n".join(pages[number] for number in sorted(pages))
        output["entities"], output["_unmapped"] = await _extract_for_text(text, use_model=use_model)
    _write_json(doc_dir(doc.id) / "entities.json", output)
    return output


def build_entity_payload(doc: Document) -> dict[str, Any] | None:
    data = _read_json(doc_dir(doc.id) / "entities.json", None)
    if not isinstance(data, dict):
        return None
    return {key: value for key, value in data.items() if not key.startswith("_")}


def validate_payload(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Mirror the current client validator's hard errors and useful warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    filename = data.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        errors.append("缺 filename(源 PDF 完整文件名)")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("缺 title(书名)")
    if "pages" in data:
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            errors.append("缺 pages(按页正文数组)")
        else:
            nums: list[int] = []
            for index, page in enumerate(pages, 1):
                if not isinstance(page, dict):
                    errors.append(f"pages[{index}] 不是对象")
                    continue
                number = page.get("page_no")
                if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                    errors.append(f"pages[{index}].page_no 非法")
                else:
                    nums.append(number)
                if not isinstance(page.get("content"), str):
                    errors.append(f"pages[{index}].content 应是字符串")
            expected = list(range(1, int(data.get("page_count") or max(nums, default=0)) + 1))
            if sorted(nums) != expected:
                errors.append("pages 页号必须连续覆盖 1..page_count，且不缺不重")
    elif "content" in data:
        errors.append("旧版 content 已废弃，请改用 pages")
    elif "entities" not in data:
        errors.append("既无 pages 也无 entities")
    for index, paper in enumerate(data.get("papers") or [], 1):
        if not isinstance(paper, dict) or not str(paper.get("title") or "").strip():
            errors.append(f"papers[{index}] 缺 title")
        if "pages" in data:
            for key in ("page_start", "page_end"):
                if not isinstance(paper.get(key), int):
                    errors.append(f"papers[{index}] 缺 {key}")
    meta = data.get("meta")
    if not isinstance(meta, dict):
        warnings.append("建议补 meta")
    elif not str(meta.get("submitter") or meta.get("extractor") or "").strip():
        warnings.append("建议填写提交人")
    return errors, warnings


def export_document(
    doc: Document,
    *,
    submitter: str,
    output_root: Path | None = None,
) -> dict[str, Any]:
    ocr = build_ocr_payload(doc, submitter=submitter)
    root = output_root or (settings.data_root / "提交")
    person_root = root / (submitter.strip() or "未命名")
    stem = Path(str(ocr["filename"])).stem
    ocr_path = person_root / "OCR" / f"{stem}.json"
    _write_json(ocr_path, ocr)
    ocr_errors, ocr_warnings = validate_payload(ocr)
    result: dict[str, Any] = {
        "document_id": doc.id,
        "ocr_path": str(ocr_path),
        "entity_path": None,
        "errors": ocr_errors,
        "warnings": ocr_warnings,
    }
    entity = build_entity_payload(doc)
    if entity is not None:
        entity_meta = entity.setdefault("meta", {})
        if isinstance(entity_meta, dict):
            entity_meta["extractor"] = submitter.strip() or "未命名"
        entity_path = person_root / "实体" / f"{stem}.json"
        _write_json(entity_path, entity)
        entity_errors, entity_warnings = validate_payload(entity)
        result["entity_path"] = str(entity_path)
        result["errors"].extend(entity_errors)
        result["warnings"].extend(entity_warnings)
    return result
