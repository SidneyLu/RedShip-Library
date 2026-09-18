from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ocr_app.config import settings
from ocr_app.db.models import Document, OcrJob
from ocr_app.library.paths import (
    abs_from_relative,
    copy_pdf,
    doc_dir,
    doc_relative_pdf,
    doc_relative_thumb,
    metadata_json,
    new_id,
    parse_metadata,
    remove_doc_tree,
    sha256_file,
)
from ocr_app.ocr_core.vision_pdf import pdf_page_count, render_thumbnail


def _delivery_defaults(pdf_path: Path, current: dict | None = None) -> dict:
    meta = dict(current or {})
    meta.setdefault("original_filename", pdf_path.name)
    match = re.search(r"第\s*(\d+)\s*辑", pdf_path.stem)
    if match and meta.get("volume") is None:
        meta["volume"] = int(match.group(1))
    meta.setdefault("proofread", False)
    return meta


def document_to_dict(doc: Document, *, job: OcrJob | None = None) -> dict:
    meta = parse_metadata(doc.extra_metadata)
    data: dict = {
        "id": doc.id,
        "title": doc.title,
        "source_path": doc.source_path,
        "relative_path": doc.relative_path,
        "file_sha256": doc.file_sha256,
        "pages": doc.pages,
        "block_count": doc.block_count,
        "review_score": doc.review_score,
        "review_summary": doc.review_summary,
        "status": doc.status,
        "series": doc.series,
        "era": doc.era,
        "dpi": doc.dpi,
        "parser": doc.parser,
        "vision_model": doc.vision_model,
        "error": doc.error,
        "extra_metadata": meta,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "ocr_job": None,
    }
    if job is not None:
        data["ocr_job"] = {
            "id": job.id,
            "status": job.status,
            "current_page": job.current_page,
            "total_pages": job.total_pages,
            "error": job.error,
        }
    return data


async def latest_ocr_job(session: AsyncSession, doc_id: str) -> OcrJob | None:
    return await session.scalar(
        select(OcrJob)
        .where(OcrJob.document_id == doc_id)
        .order_by(OcrJob.started_at.desc())
        .limit(1)
    )


async def backfill_delivery_metadata(session: AsyncSession) -> int:
    """Populate portable source filename/volume defaults for existing libraries."""
    documents = (await session.scalars(select(Document))).all()
    changed = 0
    for doc in documents:
        meta = parse_metadata(doc.extra_metadata)
        source = Path(doc.source_path) if doc.source_path else Path(f"{doc.title}.pdf")
        updated = _delivery_defaults(source, meta)
        if updated != meta:
            doc.extra_metadata = metadata_json(updated)
            changed += 1
    if changed:
        await session.commit()
    return changed


async def _jobs_for_running_docs(
    session: AsyncSession, doc_ids: list[str]
) -> dict[str, OcrJob]:
    if not doc_ids:
        return {}
    stmt = (
        select(OcrJob)
        .where(OcrJob.document_id.in_(doc_ids))
        .order_by(OcrJob.started_at.desc())
    )
    rows = (await session.scalars(stmt)).all()
    out: dict[str, OcrJob] = {}
    for job in rows:
        if job.document_id not in out:
            out[job.document_id] = job
    return out


async def list_documents(
    session: AsyncSession,
    *,
    q: str | None = None,
    status: str | None = None,
    series: str | None = None,
    uncategorized: bool = False,
    sort: str = "updated_at",
    limit: int = 20000,
) -> tuple[list[dict], int]:
    """Return (items, total) where total is the filtered count before limit."""
    filters = []
    if status:
        filters.append(Document.status == status)
    if uncategorized:
        filters.append((Document.series.is_(None)) | (Document.series == ""))
    elif series:
        filters.append(Document.series == series)
    if q:
        like = f"%{q}%"
        filters.append(Document.title.like(like))

    count_stmt = select(func.count()).select_from(Document)
    stmt = select(Document)
    for clause in filters:
        count_stmt = count_stmt.where(clause)
        stmt = stmt.where(clause)

    total = int((await session.scalar(count_stmt)) or 0)

    if sort == "title":
        stmt = stmt.order_by(Document.title)
    elif sort == "created_at":
        stmt = stmt.order_by(Document.created_at.desc())
    else:
        stmt = stmt.order_by(Document.updated_at.desc())
    stmt = stmt.limit(max(1, int(limit)))
    rows = (await session.scalars(stmt)).all()
    running_ids = [d.id for d in rows if d.status == "ocr_running"]
    jobs_map = await _jobs_for_running_docs(session, running_ids)
    items = [document_to_dict(d, job=jobs_map.get(d.id)) for d in rows]
    return items, total


async def get_document(session: AsyncSession, doc_id: str) -> Document | None:
    return await session.get(Document, doc_id)


async def get_document_dict(session: AsyncSession, doc_id: str) -> dict | None:
    doc = await get_document(session, doc_id)
    if not doc:
        return None
    job = await latest_ocr_job(session, doc_id) if doc.status == "ocr_running" else None
    return document_to_dict(doc, job=job)


def _copy_pdf_to_library(pdf_path: Path, dest: Path, *, copy: bool) -> None:
    if copy:
        copy_pdf(pdf_path, dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, dest)


def _finalize_import_sync(doc_id: str) -> int:
    dest = abs_from_relative(doc_relative_pdf(doc_id))
    pages = pdf_page_count(dest) if dest.is_file() else 0
    thumb = abs_from_relative(doc_relative_thumb(doc_id))
    try:
        render_thumbnail(dest, thumb)
    except Exception:
        pass
    return pages


async def finalize_import(doc_id: str) -> None:
    """Background: page count + thumbnail after fast import."""
    from ocr_app.db.session import get_session_factory

    try:
        pages = await asyncio.to_thread(_finalize_import_sync, doc_id)
        factory = get_session_factory()
        async with factory() as session:
            doc = await session.get(Document, doc_id)
            if doc and doc.pages == 0:
                doc.pages = pages
                await session.commit()
    except Exception:
        pass


async def import_pdf(
    session: AsyncSession,
    *,
    pdf_path: Path,
    title: str | None = None,
    series: str | None = None,
    era: str | None = None,
    copy: bool = True,
    defer_finalize: bool = True,
) -> Document:
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(str(pdf_path))

    sha = await asyncio.to_thread(sha256_file, pdf_path)
    resolved_title = (title or pdf_path.stem).strip() or pdf_path.stem
    existing = await session.scalar(select(Document).where(Document.file_sha256 == sha))
    if existing:
        meta = _delivery_defaults(pdf_path, parse_metadata(existing.extra_metadata))
        existing.extra_metadata = metadata_json(meta)
        if resolved_title and existing.title != resolved_title:
            existing.title = resolved_title
            existing.source_path = str(pdf_path)
        await session.commit()
        await session.refresh(existing)
        return existing

    doc_id = new_id()
    rel = doc_relative_pdf(doc_id)
    dest = abs_from_relative(rel)
    await asyncio.to_thread(_copy_pdf_to_library, pdf_path, dest, copy=copy)

    doc = Document(
        id=doc_id,
        title=resolved_title,
        source_path=str(pdf_path),
        relative_path=rel,
        file_sha256=sha,
        pages=0,
        status="pending",
        series=series,
        era=era,
        parser="vision_pdf",
        extra_metadata=metadata_json(_delivery_defaults(pdf_path)),
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    if defer_finalize:
        asyncio.create_task(finalize_import(doc_id))
    else:
        pages = await asyncio.to_thread(_finalize_import_sync, doc_id)
        doc.pages = pages
        await session.commit()
        await session.refresh(doc)

    return doc


def _import_artifacts_sync(
    doc_id: str,
    *,
    layout_path: Path | None,
    md_path: Path | None,
    review_path: Path | None,
) -> dict:
    """Copy sidecar artifacts; return metadata updates."""
    ddir = doc_dir(doc_id)
    updates: dict = {"pages": 0, "block_count": 0, "status": None, "review_score": None, "review_summary": None}
    if layout_path and layout_path.is_file():
        shutil.copy2(layout_path, ddir / "layout.json")
        try:
            layout = json.loads((ddir / "layout.json").read_text(encoding="utf-8"))
            updates["pages"] = len(layout.get("pages") or [])
            updates["block_count"] = sum(len(p.get("blocks") or []) for p in layout.get("pages") or [])
        except json.JSONDecodeError:
            pass
    if md_path and md_path.is_file():
        shutil.copy2(md_path, ddir / "content.md")
    if review_path and review_path.is_file():
        shutil.copy2(review_path, ddir / "review.json")
        try:
            review = json.loads((ddir / "review.json").read_text(encoding="utf-8"))
            updates["review_score"] = float(review.get("score", 0))
            updates["review_summary"] = str(review.get("summary") or "")[:512]
            updates["status"] = "needs_rerun" if review.get("needs_rerun") else "ready"
        except (json.JSONDecodeError, TypeError, ValueError):
            updates["status"] = "ready"
    elif layout_path:
        updates["status"] = "ready"
    return updates


async def import_existing_artifacts(
    session: AsyncSession,
    *,
    pdf_path: Path,
    layout_path: Path | None,
    md_path: Path | None,
    source_path: str | None = None,
    title: str | None = None,
) -> str:
    doc = await import_pdf(session, pdf_path=pdf_path, title=title, copy=True, defer_finalize=True)
    review_path = pdf_path.parent / "review.json"
    updates = await asyncio.to_thread(
        _import_artifacts_sync,
        doc.id,
        layout_path=layout_path,
        md_path=md_path,
        review_path=review_path if review_path.is_file() else None,
    )
    if updates["pages"]:
        doc.pages = max(doc.pages, updates["pages"])
    if updates["block_count"]:
        doc.block_count = updates["block_count"]
    if updates["review_score"] is not None:
        doc.review_score = updates["review_score"]
        doc.review_summary = updates["review_summary"]
    if updates["status"]:
        doc.status = updates["status"]
    doc.source_path = source_path or str(pdf_path)
    await session.commit()
    return doc.id


async def update_document(
    session: AsyncSession,
    doc_id: str,
    *,
    title: str | None = None,
    series: str | None = None,
    era: str | None = None,
    clear_series: bool = False,
    delivery_metadata: dict | None = None,
) -> Document | None:
    doc = await get_document(session, doc_id)
    if not doc:
        return None
    if title is not None:
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("title required")
        doc.title = cleaned
    if clear_series:
        doc.series = None
    elif series is not None:
        name = series.strip()
        if name:
            from ocr_app.library.folders import ensure_folder_name

            doc.series = ensure_folder_name(name)
        else:
            doc.series = None
    if era is not None:
        doc.era = era
    if delivery_metadata is not None:
        meta = parse_metadata(doc.extra_metadata)
        allowed = {
            "original_filename",
            "district",
            "volume",
            "pub_year",
            "pub_org",
            "proofread",
            "delivery_notes",
        }
        for key, value in delivery_metadata.items():
            if key in allowed:
                meta[key] = value
        doc.extra_metadata = metadata_json(meta)
    await session.commit()
    await session.refresh(doc)
    return doc


async def delete_document(session: AsyncSession, doc_id: str) -> bool:
    doc = await get_document(session, doc_id)
    if not doc:
        return False
    await session.delete(doc)
    await session.commit()
    await asyncio.to_thread(remove_doc_tree, doc_id)
    return True


def artifact_path(doc_id: str, name: str) -> Path | None:
    p = doc_dir(doc_id) / name
    return p if p.is_file() else None


async def mark_document_running(session: AsyncSession, doc: Document, dpi: int) -> None:
    doc.status = "ocr_running"
    doc.dpi = dpi
    doc.error = None
    await session.commit()


async def mark_document_review_running(session: AsyncSession, doc: Document) -> None:
    doc.status = "review_running"
    doc.error = None
    await session.commit()


async def mark_document_ocr_done(
    session: AsyncSession,
    doc: Document,
    *,
    pages: int,
    block_count: int,
    vision_model: str,
) -> None:
    """OCR finished; awaiting separate review."""
    doc.pages = pages
    doc.block_count = block_count
    doc.vision_model = vision_model
    doc.status = "ocr_done"
    doc.error = None
    doc.extra_metadata = metadata_json(
        {
            **parse_metadata(doc.extra_metadata),
            "vision_model": vision_model,
            "pages": pages,
            "block_count": block_count,
        }
    )
    await session.commit()


async def mark_document_ready(
    session: AsyncSession,
    doc: Document,
    *,
    pages: int,
    block_count: int,
    review_score: float | None,
    review_summary: str | None,
    vision_model: str,
    needs_rerun: bool,
) -> None:
    doc.pages = pages
    doc.block_count = block_count
    doc.review_score = review_score
    doc.review_summary = (review_summary or "")[:512] if review_summary else None
    doc.vision_model = vision_model
    doc.status = "needs_rerun" if needs_rerun else "ready"
    doc.error = None
    doc.extra_metadata = metadata_json(
        {
            **parse_metadata(doc.extra_metadata),
            "vision_model": vision_model,
            "pages": pages,
            "block_count": block_count,
        }
    )
    await session.commit()


async def mark_document_failed(session: AsyncSession, doc: Document, error: str) -> None:
    doc.status = "failed"
    doc.error = error[:2000]
    await session.commit()


async def mark_document_partial(
    session: AsyncSession,
    doc: Document,
    error: str,
    *,
    block_count: int,
) -> None:
    doc.status = "partial"
    doc.error = error[:2000]
    doc.block_count = block_count
    await session.commit()
