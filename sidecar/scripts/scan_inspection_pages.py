#!/usr/bin/env python3
"""Scan Library docs for OCR placeholder pages (inspection / disconnect / empty).

Writes rerun_inspection_queue.json under --data-root.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

MARKERS = (
    "【本页因云端内容安全审核未能识别",
    "【本页因网络中断未能识别",
    "【本页模型返回空结果未能识别",
)


def page_placeholder_nums(layout: dict) -> list[int]:
    pages: list[int] = []
    for page in layout.get("pages") or []:
        pnum = int(page.get("page") or 0)
        if pnum < 1:
            continue
        texts = []
        for b in page.get("blocks") or []:
            if isinstance(b, dict):
                texts.append(str(b.get("text") or ""))
        blob = "\n".join(texts)
        if any(m in blob for m in MARKERS):
            pages.append(pnum)
    return pages


def scan_docs(docs_root: Path) -> list[dict]:
    out: list[dict] = []
    if not docs_root.is_dir():
        return out
    for ddir in sorted(p for p in docs_root.iterdir() if p.is_dir()):
        layout_path = ddir / "layout.json"
        md_path = ddir / "content.md"
        pages: list[int] = []
        kinds: set[str] = set()
        if layout_path.is_file():
            try:
                layout = json.loads(layout_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                layout = {}
            pages = page_placeholder_nums(layout if isinstance(layout, dict) else {})
            for page in (layout.get("pages") if isinstance(layout, dict) else None) or []:
                for b in page.get("blocks") or []:
                    text = str((b or {}).get("text") or "")
                    if MARKERS[0] in text:
                        kinds.add("inspection")
                    if MARKERS[1] in text:
                        kinds.add("disconnect")
                    if MARKERS[2] in text:
                        kinds.add("empty")
        elif md_path.is_file():
            text = md_path.read_text(encoding="utf-8", errors="replace")
            if any(m in text for m in MARKERS):
                # Markdown-only: cannot resolve page nums reliably — flag whole doc
                pages = []
                if MARKERS[0] in text:
                    kinds.add("inspection")
                if MARKERS[1] in text:
                    kinds.add("disconnect")
                if MARKERS[2] in text:
                    kinds.add("empty")
                kinds.add("markdown_only")
        if pages or ("markdown_only" in kinds):
            out.append(
                {
                    "document_id": ddir.name,
                    "pages": pages,
                    "kinds": sorted(kinds),
                    "page_count": len(pages),
                }
            )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan OCR inspection/placeholder pages")
    parser.add_argument("--data-root", default="/root/autodl-tmp/Library")
    parser.add_argument(
        "--out",
        default="",
        help="Output queue JSON (default: <data-root>/rerun_inspection_queue.json)",
    )
    parser.add_argument(
        "--kinds",
        default="inspection,disconnect,empty",
        help="Comma filter: inspection,disconnect,empty",
    )
    args = parser.parse_args()
    data_root = Path(args.data_root)
    want = {k.strip() for k in args.kinds.split(",") if k.strip()}
    items = scan_docs(data_root / "docs")
    if want:
        filtered = []
        for item in items:
            kinds = set(item.get("kinds") or [])
            if kinds & want or ("markdown_only" in kinds and kinds & want):
                filtered.append(item)
        items = filtered
    out_path = Path(args.out) if args.out else data_root / "reports" / "rerun_inspection_queue.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "data_root": str(data_root),
        "document_count": len(items),
        "page_count": sum(int(x.get("page_count") or 0) for x in items),
        "items": items,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"docs={payload['document_count']} pages={payload['page_count']} -> {out_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
