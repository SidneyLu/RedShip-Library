from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ocr_app.config import settings
from ocr_app.db.models import Document
from ocr_app.library.paths import doc_relative_pdf, sha256_file
from ocr_app.library.scan import (
    infer_status_from_artifacts,
    load_layout_from_dir,
    read_review_from_dir,
)
from ocr_app.ocr_core.vision_pdf import pdf_page_count, render_thumbnail


@dataclass
class ReindexReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skipped_duplicate_sha: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


@dataclass
class MergeReport:
    copied: list[str] = field(default_factory=list)
    skipped_same: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    reindex: ReindexReport | None = None


def _is_valid_uuid(name: str) -> bool:
    try:
        uuid.UUID(name)
        return True
    except ValueError:
        return False


def _docs_root() -> Path:
    return settings.data_root / "docs"


def _iter_doc_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "source.pdf").is_file():
            out.append(child)
    return out


async def _upsert_from_dir(session: AsyncSession, ddir: Path, report: ReindexReport) -> None:
    doc_id = ddir.name
    pdf_path = ddir / "source.pdf"
    if not _is_valid_uuid(doc_id):
        report.invalid.append(str(ddir))
        return

    def _read_meta() -> tuple[str, dict | None, dict | None, int, str, int, int, float | None, str | None]:
        sha = sha256_file(pdf_path)
        layout = load_layout_from_dir(ddir)
        review = read_review_from_dir(ddir)
        pdf_pages = pdf_page_count(pdf_path)
        status, pages, block_count, review_score, review_summary = infer_status_from_artifacts(
            layout=layout,
            review=review,
            pdf_pages=pdf_pages,
        )
        return sha, layout, review, pdf_pages, status, pages, block_count, review_score, review_summary

    sha, _layout, _review, _pdf_pages, status, pages, block_count, review_score, review_summary = (
        await asyncio.to_thread(_read_meta)
    )

    existing_by_id = await session.get(Document, doc_id)
    existing_by_sha = await session.scalar(
        select(Document).where(Document.file_sha256 == sha)
    )

    rel = doc_relative_pdf(doc_id)
    title = pdf_path.stem if pdf_path.stem != "source" else doc_id

    if existing_by_id:
        existing_by_id.relative_path = rel
        existing_by_id.file_sha256 = sha
        existing_by_id.pages = pages
        existing_by_id.block_count = block_count
        existing_by_id.review_score = review_score
        existing_by_id.review_summary = review_summary
        existing_by_id.status = status
        existing_by_id.source_path = str(pdf_path)
        if not existing_by_id.title or existing_by_id.title == existing_by_id.id:
            existing_by_id.title = title
        report.updated.append(doc_id)
        return

    if existing_by_sha and existing_by_sha.id != doc_id:
        report.skipped_duplicate_sha.append(doc_id)
        return

    thumb = ddir / "thumb.png"
    if not thumb.is_file():
        await asyncio.to_thread(_try_render_thumb, pdf_path, thumb)

    doc = Document(
        id=doc_id,
        title=title,
        source_path=str(pdf_path),
        relative_path=rel,
        file_sha256=sha,
        pages=pages,
        block_count=block_count,
        review_score=review_score,
        review_summary=review_summary,
        status=status,
        parser="vision_pdf",
    )
    session.add(doc)
    report.added.append(doc_id)


def _try_render_thumb(pdf_path: Path, thumb: Path) -> None:
    try:
        render_thumbnail(pdf_path, thumb)
    except Exception:
        pass


async def reindex_from_docs(session: AsyncSession) -> ReindexReport:
    report = ReindexReport()
    for ddir in _iter_doc_dirs(_docs_root()):
        await _upsert_from_dir(session, ddir, report)
    await session.commit()
    return report


async def merge_external_docs(
    session: AsyncSession,
    source: Path,
    *,
    run_reindex: bool = True,
) -> MergeReport:
    source = source.resolve()
    report = MergeReport()

    if not source.is_dir():
        raise FileNotFoundError(str(source))

    # Accept either docs/ root or a folder containing uuid subdirs
    if (source / "source.pdf").is_file() and _is_valid_uuid(source.name):
        candidates = [source]
    else:
        candidates = _iter_doc_dirs(source)

    dest_root = _docs_root()
    dest_root.mkdir(parents=True, exist_ok=True)

    for src_dir in candidates:
        doc_id = src_dir.name
        src_pdf = src_dir / "source.pdf"
        if not _is_valid_uuid(doc_id) or not src_pdf.is_file():
            report.invalid.append(str(src_dir))
            continue

        dest_dir = dest_root / doc_id

        def _merge_one() -> str:
            src_sha = sha256_file(src_pdf)
            if not dest_dir.exists():
                shutil.copytree(src_dir, dest_dir)
                return "copied"
            dest_pdf = dest_dir / "source.pdf"
            if not dest_pdf.is_file():
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
                return "copied"
            dest_sha = sha256_file(dest_pdf)
            if src_sha == dest_sha:
                return "skipped_same"
            return "conflict"

        action = await asyncio.to_thread(_merge_one)
        if action == "copied":
            report.copied.append(doc_id)
        elif action == "skipped_same":
            report.skipped_same.append(doc_id)
        else:
            report.conflicts.append(doc_id)

    if run_reindex:
        report.reindex = await reindex_from_docs(session)
    else:
        await session.commit()

    return report
