"""SQLite FTS5 trigram index over OCR content.md (page-level rows)."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ocr_app.db.models import Document
from ocr_app.db.session import get_session_factory
from ocr_app.library.paths import doc_dir

_PAGE_MARKER = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
_FTS_SPECIAL = re.compile(r'["\'*\^]')

_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
  document_id UNINDEXED,
  page UNINDEXED,
  title,
  series,
  body,
  tokenize='trigram'
)
"""

_reindex_lock = threading.Lock()
_reindex_state: dict[str, Any] = {
    "status": "idle",
    "current": 0,
    "total": 0,
    "indexed": 0,
    "skipped": 0,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def ensure_fts_table_sync(conn) -> None:
    conn.execute(_DDL)


async def ensure_fts_table(session: AsyncSession | None = None) -> None:
    if session is not None:
        await session.execute(text(_DDL))
        await session.commit()
        return
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(text(_DDL))
        await s.commit()


def parse_content_pages(markdown: str) -> list[tuple[int, str]]:
    """Split content.md into (page_num, body) using <!-- page: N --> markers."""
    text_md = (markdown or "").strip()
    if not text_md:
        return []
    matches = list(_PAGE_MARKER.finditer(text_md))
    if not matches:
        body = text_md
        # Drop leading # title line if present
        lines = body.splitlines()
        if lines and lines[0].startswith("# "):
            body = "\n".join(lines[1:]).strip()
        return [(1, body)] if body else []

    pages: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        page_num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text_md)
        body = text_md[start:end].strip()
        if body:
            pages.append((page_num, body))
    return pages


def sanitize_fts_query(q: str) -> str:
    """Strip FTS5 operators; return literal token for trigram MATCH."""
    cleaned = _FTS_SPECIAL.sub(" ", (q or "").strip())
    cleaned = " ".join(cleaned.split())
    return cleaned


def fts_match_expr(q: str) -> str:
    """Quote sanitized query so multi-char Chinese matches as a phrase."""
    cleaned = sanitize_fts_query(q)
    if not cleaned:
        return ""
    # Escape remaining double-quotes just in case
    cleaned = cleaned.replace('"', "")
    return f'"{cleaned}"'


def get_reindex_status() -> dict[str, Any]:
    with _reindex_lock:
        return dict(_reindex_state)


def _set_reindex(**kwargs: Any) -> None:
    with _reindex_lock:
        _reindex_state.update(kwargs)


async def fts_row_count(session: AsyncSession) -> int:
    try:
        n = await session.scalar(text("SELECT COUNT(*) FROM doc_fts"))
        return int(n or 0)
    except Exception:
        return 0


async def delete_document_fts(session: AsyncSession, document_id: str) -> None:
    await session.execute(
        text("DELETE FROM doc_fts WHERE document_id = :id"),
        {"id": document_id},
    )


async def upsert_document(
    session: AsyncSession,
    document_id: str,
    *,
    title: str | None = None,
    series: str | None = None,
) -> int:
    """Re-index one document from content.md. Returns number of page rows written."""
    md_path = doc_dir(document_id) / "content.md"
    if not md_path.is_file():
        await delete_document_fts(session, document_id)
        return 0

    if title is None or series is None:
        doc = await session.get(Document, document_id)
        if doc is not None:
            title = title if title is not None else doc.title
            series = series if series is not None else (doc.series or "")
        else:
            title = title or ""
            series = series or ""

    try:
        markdown = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("FTS read failed for {}: {}", document_id, exc)
        return 0

    pages = parse_content_pages(markdown)
    await delete_document_fts(session, document_id)
    if not pages:
        return 0

    for page_num, body in pages:
        await session.execute(
            text(
                "INSERT INTO doc_fts (document_id, page, title, series, body) "
                "VALUES (:document_id, :page, :title, :series, :body)"
            ),
            {
                "document_id": document_id,
                "page": page_num,
                "title": title or "",
                "series": series or "",
                "body": body,
            },
        )
    return len(pages)


async def upsert_document_by_id(document_id: str) -> int:
    """Convenience: open a session, upsert, commit."""
    factory = get_session_factory()
    async with factory() as session:
        n = await upsert_document(session, document_id)
        await session.commit()
        return n


async def search_documents(
    session: AsyncSession,
    *,
    q: str,
    status: str | None = None,
    series: str | None = None,
    uncategorized: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    query = sanitize_fts_query(q)
    if not query:
        return [], 0

    # Trigram needs at least 3 characters for MATCH; fall back handled by caller for shorter.
    if len(query) < 3:
        return [], 0

    match_q = fts_match_expr(query)
    filters = ["doc_fts MATCH :q"]
    params: dict[str, Any] = {"q": match_q}
    if status:
        filters.append("d.status = :status")
        params["status"] = status
    if uncategorized:
        filters.append("(d.series IS NULL OR d.series = '')")
    elif series:
        filters.append("d.series = :series")
        params["series"] = series

    where = " AND ".join(filters)
    count_sql = text(
        f"""
        SELECT COUNT(*)
        FROM doc_fts
        JOIN documents d ON d.id = doc_fts.document_id
        WHERE {where}
        """
    )
    total = int((await session.scalar(count_sql, params)) or 0)

    params["limit"] = max(1, int(limit))
    params["offset"] = max(0, int(offset))
    rows_sql = text(
        f"""
        SELECT
          doc_fts.document_id AS document_id,
          d.title AS title,
          d.series AS series,
          d.status AS status,
          doc_fts.page AS page,
          snippet(doc_fts, 4, '«', '»', '…', 40) AS snippet
        FROM doc_fts
        JOIN documents d ON d.id = doc_fts.document_id
        WHERE {where}
        ORDER BY rank
        LIMIT :limit OFFSET :offset
        """
    )
    result = await session.execute(rows_sql, params)
    items = [
        {
            "document_id": str(r.document_id),
            "title": r.title,
            "series": r.series,
            "status": r.status,
            "page": int(r.page),
            "snippet": r.snippet or "",
        }
        for r in result.all()
    ]
    return items, total


async def rebuild_all(*, force: bool = False) -> dict[str, Any]:
    """Full rebuild of doc_fts. Skips if already running unless force clears state."""
    with _reindex_lock:
        if _reindex_state["status"] == "running" and not force:
            return dict(_reindex_state)

    _set_reindex(
        status="running",
        current=0,
        total=0,
        indexed=0,
        skipped=0,
        error=None,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        finished_at=None,
    )

    factory = get_session_factory()
    indexed = 0
    skipped = 0
    rows: list[Any] = []
    try:
        async with factory() as session:
            await ensure_fts_table(session)
            await session.execute(text("DELETE FROM doc_fts"))
            await session.commit()

            result = await session.execute(
                text("SELECT id, title, series FROM documents ORDER BY updated_at DESC")
            )
            rows = list(result.all())
            _set_reindex(total=len(rows))

            for i, row in enumerate(rows, 1):
                doc_id, title, series = str(row[0]), str(row[1] or ""), str(row[2] or "")
                md_path = doc_dir(doc_id) / "content.md"
                if not md_path.is_file():
                    skipped += 1
                    _set_reindex(current=i, skipped=skipped)
                    continue
                n = await upsert_document(
                    session, doc_id, title=title, series=series
                )
                if n:
                    indexed += 1
                else:
                    skipped += 1
                if i % 25 == 0:
                    await session.commit()
                _set_reindex(current=i, indexed=indexed, skipped=skipped)
            await session.commit()

        _set_reindex(
            status="done",
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            indexed=indexed,
            skipped=skipped,
            current=len(rows),
        )
    except Exception as exc:
        logger.exception("FTS rebuild failed: {}", exc)
        _set_reindex(
            status="error",
            error=str(exc),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

    return get_reindex_status()


def start_rebuild_background(*, force: bool = False) -> dict[str, Any]:
    with _reindex_lock:
        if _reindex_state["status"] == "running" and not force:
            return dict(_reindex_state)
        _reindex_state.update(
            {
                "status": "running",
                "current": 0,
                "total": 0,
                "indexed": 0,
                "skipped": 0,
                "error": None,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "finished_at": None,
            }
        )

    async def _run() -> None:
        await rebuild_all(force=True)

    def _thread() -> None:
        asyncio.run(_run())

    t = threading.Thread(target=_thread, name="fts-reindex", daemon=True)
    t.start()
    return get_reindex_status()


async def maybe_auto_reindex_on_startup() -> None:
    """If FTS is empty but content.md files exist, rebuild in background."""
    factory = get_session_factory()
    async with factory() as session:
        await ensure_fts_table(session)
        count = await fts_row_count(session)
        if count > 0:
            return
        docs_root = Path(doc_dir("_").parent)  # data_root/docs
        has_md = False
        if docs_root.is_dir():
            for child in docs_root.iterdir():
                if child.is_dir() and (child / "content.md").is_file():
                    has_md = True
                    break
        if not has_md:
            return
    logger.info("FTS index empty; starting background rebuild")
    start_rebuild_background()
