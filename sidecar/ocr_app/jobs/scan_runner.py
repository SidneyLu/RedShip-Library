from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select

from ocr_app.db.models import Document, ScanJob
from ocr_app.db.session import get_session_factory
from ocr_app.library.scan import ScanResult, find_pdfs
from ocr_app.library.service import import_existing_artifacts


class ScanJobManager:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._running: set[str] = set()

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._events[job_id] = q
        return q

    def unsubscribe(self, job_id: str) -> None:
        self._events.pop(job_id, None)

    async def _emit(self, job_id: str, event: dict[str, Any]) -> None:
        q = self._events.get(job_id)
        if q:
            await q.put(event)

    async def run_scan(
        self,
        *,
        scan_job_id: str,
        folder: Path,
        recursive: bool,
        run_ocr: bool,
        ocr_pending_in_library: bool,
        ocr_statuses: list[str],
        dpi: int | None,
        max_pages: int | None,
        queue_ocr_fn,
    ) -> None:
        if scan_job_id in self._running:
            return
        self._running.add(scan_job_id)
        factory = get_session_factory()

        try:
            pdfs = await asyncio.to_thread(find_pdfs, folder, recursive=recursive)
            total = len(pdfs)

            async with factory() as session:
                job = await session.get(ScanJob, scan_job_id)
                if job:
                    job.total = total
                    await session.commit()

            await self._emit(
                scan_job_id,
                {"type": "progress", "current": 0, "total": total, "phase": "scan"},
            )

            result = ScanResult()
            for idx, pdf in enumerate(pdfs, start=1):
                stem = pdf.stem
                layout = pdf.parent / f"{stem}.layout.json"
                md = pdf.parent / f"{stem}.md"
                if not layout.is_file():
                    layout = pdf.parent / "layout.json"
                if not md.is_file():
                    md = pdf.parent / "content.md"
                layout_path = layout if layout.is_file() else None
                md_path = md if md.is_file() else None

                async with factory() as session:
                    from ocr_app.library.paths import sha256_file

                    sha = await asyncio.to_thread(sha256_file, pdf)
                    existing = await session.scalar(
                        select(Document).where(Document.file_sha256 == sha)
                    )
                    if existing:
                        result.skipped_sha.append(existing.id)
                    else:
                        doc_id = await import_existing_artifacts(
                            session,
                            pdf_path=pdf,
                            layout_path=layout_path,
                            md_path=md_path,
                            source_path=str(pdf),
                        )
                        result.imported.append(doc_id)

                    job = await session.get(ScanJob, scan_job_id)
                    if job:
                        job.current = idx
                        await session.commit()

                await self._emit(
                    scan_job_id,
                    {
                        "type": "progress",
                        "current": idx,
                        "total": total,
                        "file": pdf.name,
                        "phase": "scan",
                    },
                )

            queued_ocr: list[str] = []
            skipped_ocr: list[str] = []
            if run_ocr or ocr_pending_in_library:
                async with factory() as session:
                    ocr_targets: list[str] = []
                    if run_ocr:
                        ocr_targets.extend(result.imported)
                    if ocr_pending_in_library:
                        rows = (
                            await session.scalars(
                                select(Document.id).where(Document.status.in_(ocr_statuses))
                            )
                        ).all()
                        ocr_targets.extend(rows)
                    jobs, skipped = await queue_ocr_fn(
                        session, ocr_targets, dpi=dpi, max_pages=max_pages
                    )
                    queued_ocr = [j["document_id"] for j in jobs]
                    skipped_ocr = skipped

            payload = {
                "imported": result.imported,
                "count": len(result.imported),
                "skipped_sha_count": len(result.skipped_sha),
                "queued_ocr_count": len(queued_ocr),
                "skipped_ocr": skipped_ocr,
            }

            async with factory() as session:
                job = await session.get(ScanJob, scan_job_id)
                if job:
                    job.status = "done"
                    job.result_json = json.dumps(payload, ensure_ascii=False)
                    job.finished_at = datetime.now(timezone.utc)
                    await session.commit()

            await self._emit(scan_job_id, {"type": "done", **payload})

        except Exception as e:
            logger.exception("Scan job failed: {}", e)
            async with factory() as session:
                job = await session.get(ScanJob, scan_job_id)
                if job:
                    job.status = "failed"
                    job.error = str(e)[:2000]
                    job.finished_at = datetime.now(timezone.utc)
                    await session.commit()
            await self._emit(scan_job_id, {"type": "error", "error": str(e)})
        finally:
            self._running.discard(scan_job_id)


scan_job_manager = ScanJobManager()
