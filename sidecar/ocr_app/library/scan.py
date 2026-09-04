from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from ocr_app.config import settings
from ocr_app.library.paths import sha256_file


def _is_under_docs(path: Path) -> bool:
    docs_root = (settings.data_root / "docs").resolve()
    try:
        path.resolve().relative_to(docs_root)
        return True
    except ValueError:
        return False


def find_pdfs(folder: Path, *, recursive: bool = False) -> list[Path]:
    """Find PDF files in folder, excluding files under data_root/docs/."""
    if not folder.is_dir():
        return []
    if recursive:
        candidates = folder.rglob("*.pdf")
    else:
        candidates = folder.glob("*.pdf")
    out: list[Path] = []
    for pdf in sorted(candidates):
        if not pdf.is_file():
            continue
        if _is_under_docs(pdf):
            continue
        out.append(pdf)
    return out


def find_pdf_artifacts(folder: Path) -> list[tuple[Path, Path | None, Path | None]]:
    """Return (pdf, layout.json path, md path) for each PDF in folder."""
    out: list[tuple[Path, Path | None, Path | None]] = []
    for pdf in find_pdfs(folder, recursive=False):
        stem = pdf.stem
        layout = pdf.parent / f"{stem}.layout.json"
        md = pdf.parent / f"{stem}.md"
        if not layout.is_file():
            layout = pdf.parent / "layout.json"
        if not md.is_file():
            md = pdf.parent / "content.md"
        out.append(
            (
                pdf,
                layout if layout.is_file() else None,
                md if md.is_file() else None,
            )
        )
    return out


@dataclass
class ScanResult:
    imported: list[str] = field(default_factory=list)
    skipped_sha: list[str] = field(default_factory=list)


async def scan_folder(
    session,
    folder: Path,
    *,
    recursive: bool = False,
) -> ScanResult:
    from sqlalchemy import select

    from ocr_app.db.models import Document
    from ocr_app.library.service import import_existing_artifacts

    result = ScanResult()
    pdfs = find_pdfs(folder, recursive=recursive)
    for pdf in pdfs:
        sha = await asyncio.to_thread(sha256_file, pdf)
        existing = await session.scalar(select(Document).where(Document.file_sha256 == sha))
        if existing:
            result.skipped_sha.append(existing.id)
            continue

        stem = pdf.stem
        layout = pdf.parent / f"{stem}.layout.json"
        md = pdf.parent / f"{stem}.md"
        if not layout.is_file():
            layout = pdf.parent / "layout.json"
        if not md.is_file():
            md = pdf.parent / "content.md"
        layout_path = layout if layout.is_file() else None
        md_path = md if md.is_file() else None

        doc_id = await import_existing_artifacts(
            session,
            pdf_path=pdf,
            layout_path=layout_path,
            md_path=md_path,
            source_path=str(pdf),
        )
        result.imported.append(doc_id)
    return result


def read_review_from_dir(d: Path) -> dict | None:
    p = d / "review.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def load_layout_from_dir(d: Path) -> dict | None:
    p = d / "layout.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def infer_status_from_artifacts(
    *,
    layout: dict | None,
    review: dict | None,
    pdf_pages: int,
) -> tuple[str, int, int, float | None, str | None]:
    """Return (status, pages, block_count, review_score, review_summary)."""
    pages = pdf_pages
    block_count = 0
    review_score: float | None = None
    review_summary: str | None = None
    status = "pending"

    if layout:
        layout_pages = layout.get("pages") or []
        pages = max(pages, len(layout_pages))
        block_count = sum(len(p.get("blocks") or []) for p in layout_pages)

    if review:
        review_score = float(review.get("score", 0))
        review_summary = str(review.get("summary") or "")[:512]
        status = "needs_rerun" if review.get("needs_rerun") else "ready"
    elif layout and block_count > 0:
        layout_pages = layout.get("pages") or []
        covered = sum(1 for p in layout_pages if p.get("blocks"))
        if pages > 0 and covered >= pages:
            status = "ready"
        else:
            status = "partial"

    return status, pages, block_count, review_score, review_summary
