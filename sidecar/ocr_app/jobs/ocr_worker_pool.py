"""Multiprocess OCR worker pool: page queue + shared VL limiter."""
from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from ocr_app.config import settings
from ocr_app.db.models import OcrPageJob
from ocr_app.library.paths import new_id
from ocr_app.ocr_core.vl_rate_limiter import (
    AdaptiveVLLimiter,
    MultiprocessVLLimiter,
    SharedVLLimiterState,
    set_vl_limiter,
)

# Sentinel reserved for future queue-based shutdown.
_POISON = None


def worker_processes_enabled() -> bool:
    return max(1, int(settings.ocr_worker_processes)) > 1


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _blocks_to_json(blocks: list[Any]) -> str:
    return json.dumps(
        [
            {"type": b.type, "text": b.text, "bbox": list(b.bbox), "page": int(b.page)}
            for b in blocks
        ],
        ensure_ascii=False,
    )


def blocks_from_page_result(raw: str | None) -> list[Any]:
    from ocr_app.ocr_core.vision_pdf import LayoutBlock

    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[LayoutBlock] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        bbox = item.get("bbox") or [0, 0, 0, 0]
        out.append(
            LayoutBlock(
                type=str(item.get("type") or "text"),
                text=text,
                bbox=[float(x) for x in bbox[:4]],
                page=int(item.get("page") or 1),
            )
        )
    return out


async def enqueue_page_jobs(
    *,
    document_id: str,
    job_id: str,
    pages: list[int],
    pdf_path: Path,
    dpi: int,
    keep_page_images: bool,
) -> list[str]:
    """Insert pending page jobs; returns page-job ids."""
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    ids: list[str] = []
    async with factory() as session:
        # Drop leftover rows for this OCR job (reruns / crashes).
        existing = (
            await session.execute(select(OcrPageJob).where(OcrPageJob.job_id == job_id))
        ).scalars().all()
        for row in existing:
            await session.delete(row)
        await session.flush()

        now = datetime.now(timezone.utc)
        for page_num in pages:
            pid = new_id()
            ids.append(pid)
            session.add(
                OcrPageJob(
                    id=pid,
                    document_id=document_id,
                    job_id=job_id,
                    page_num=int(page_num),
                    status="pending",
                    dpi=int(dpi),
                    pdf_path=str(pdf_path),
                    keep_page_images=1 if keep_page_images else 0,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.commit()
    logger.info(
        "Enqueued {} page job(s) for document {} job {}",
        len(ids),
        document_id,
        job_id,
    )
    return ids


async def cancel_page_jobs_for_document(document_id: str) -> int:
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(OcrPageJob)
            .where(
                OcrPageJob.document_id == document_id,
                OcrPageJob.status.in_(("pending", "running")),
            )
            .values(
                status="cancelled",
                updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)


async def cancel_page_jobs_for_job(job_id: str) -> int:
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(OcrPageJob)
            .where(
                OcrPageJob.job_id == job_id,
                OcrPageJob.status.in_(("pending", "running")),
            )
            .values(
                status="cancelled",
                updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)


async def heal_stale_page_jobs() -> int:
    """Re-queue page jobs left in running after a crash."""
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(OcrPageJob)
            .where(OcrPageJob.status == "running")
            .values(
                status="pending",
                claimed_by=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        n = int(result.rowcount or 0)
        if n:
            logger.warning("Healed {} stale running page job(s) -> pending", n)
        return n


async def fetch_page_job_updates(
    job_id: str, *, known_done: set[int]
) -> list[OcrPageJob]:
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(OcrPageJob).where(
                    OcrPageJob.job_id == job_id,
                    OcrPageJob.status.in_(("done", "failed", "cancelled")),
                )
            )
        ).scalars().all()
        return [r for r in rows if int(r.page_num) not in known_done]


async def _pending_job_count() -> int:
    from sqlalchemy import func

    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        n = (
            await session.execute(
                select(func.count())
                .select_from(OcrPageJob)
                .where(OcrPageJob.status == "pending")
            )
        ).scalar_one()
        return int(n or 0)


async def _pick_fair_document_id(session) -> str | None:
    """Prefer the document with fewest running pages so multi-doc OCR shares workers.

    FIFO-by-created_at let the first large PDF monopolize all workers. Cap each
    active document at ceil(page_concurrency / active_docs) running pages.
    """
    from sqlalchemy import case, func

    rows = (
        await session.execute(
            select(
                OcrPageJob.document_id,
                func.sum(case((OcrPageJob.status == "running", 1), else_=0)).label(
                    "running_n"
                ),
                func.sum(case((OcrPageJob.status == "pending", 1), else_=0)).label(
                    "pending_n"
                ),
                func.min(
                    case(
                        (OcrPageJob.status == "pending", OcrPageJob.created_at),
                        else_=None,
                    )
                ).label("oldest_pending"),
            )
            .where(OcrPageJob.status.in_(("pending", "running")))
            .group_by(OcrPageJob.document_id)
        )
    ).all()

    candidates: list[tuple[int, datetime, str]] = []
    for doc_id, running_n, pending_n, oldest_pending in rows:
        if int(pending_n or 0) <= 0:
            continue
        oldest = oldest_pending
        if oldest is None:
            oldest = datetime.now(timezone.utc)
        elif oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        candidates.append((int(running_n or 0), oldest, str(doc_id)))
    if not candidates:
        return None

    active_docs = len(candidates)
    total_slots = max(1, int(settings.ocr_page_concurrency))
    # Share slots across documents that currently have pending work.
    per_doc_cap = max(1, (total_slots + active_docs - 1) // active_docs)
    under_cap = [c for c in candidates if c[0] < per_doc_cap]
    pool = under_cap if under_cap else candidates
    pool.sort(key=lambda item: (item[0], item[1], item[2]))
    return pool[0][2]


async def _try_claim_fair_page(worker_id: str) -> OcrPageJob | None:
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        doc_id = await _pick_fair_document_id(session)
        if doc_id is None:
            return None
        row = (
            await session.execute(
                select(OcrPageJob)
                .where(
                    OcrPageJob.status == "pending",
                    OcrPageJob.document_id == doc_id,
                )
                .order_by(OcrPageJob.page_num.asc(), OcrPageJob.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        page_id = row.id
        result = await session.execute(
            update(OcrPageJob)
            .where(OcrPageJob.id == page_id, OcrPageJob.status == "pending")
            .values(
                status="running",
                claimed_by=worker_id,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if not result.rowcount:
            await session.rollback()
            return None
        await session.commit()
        return await session.get(OcrPageJob, page_id)


async def _claim_next_page_job(worker_id: str) -> OcrPageJob | None:
    """Claim one pending page with multi-doc fairness; retry brief claim races."""
    for _ in range(12):
        claimed = await _try_claim_fair_page(worker_id)
        if claimed is not None:
            return claimed
        # Distinguish empty queue from lost optimistic-lock race.
        if await _pending_job_count() == 0:
            return None
        await asyncio.sleep(0.01)
    return None


async def _finish_page_job(
    page_id: str,
    *,
    status: str,
    result_json: str | None = None,
    error: str | None = None,
) -> None:
    from ocr_app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(OcrPageJob)
            .where(OcrPageJob.id == page_id)
            .values(
                status=status,
                result_json=result_json,
                error=(error or "")[:2000] if error else None,
                updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()


async def _process_claimed_page(
    job: OcrPageJob,
    *,
    cancel_dict: Any,
) -> None:
    from ocr_app.jobs.runner import (
        _ocr_page_blocks,
        _skipped_connection_blocks,
    )
    from ocr_app.library.paths import doc_dir
    from ocr_app.ocr_core.dashscope_vl import _is_transient_disconnect
    from ocr_app.ocr_core.vision_pdf import render_pdf_page
    from ocr_app.ocr_core.vl_rate_limiter import vl_limiter

    if cancel_dict.get(job.document_id):
        await _finish_page_job(job.id, status="cancelled")
        return

    pdf_path = Path(job.pdf_path)
    if not pdf_path.is_file():
        await _finish_page_job(
            job.id, status="failed", error=f"pdf missing: {pdf_path}"
        )
        return

    with tempfile.TemporaryDirectory(prefix=f"ocr_w_{job.page_num}_") as tmp:
        tmp_path = Path(tmp)
        img_path = tmp_path / "page.png"
        try:
            await asyncio.to_thread(
                render_pdf_page, pdf_path, int(job.page_num), img_path, dpi=int(job.dpi)
            )
        except Exception as exc:
            logger.exception("Worker render failed page {}: {}", job.page_num, exc)
            await _finish_page_job(job.id, status="failed", error=str(exc))
            return

        if cancel_dict.get(job.document_id):
            await _finish_page_job(job.id, status="cancelled")
            return

        last_err: BaseException | None = None
        page_blocks = None
        for attempt in range(2):
            if cancel_dict.get(job.document_id):
                await _finish_page_job(job.id, status="cancelled")
                return
            try:
                page_blocks = await _ocr_page_blocks(img_path, int(job.page_num))
                break
            except Exception as exc:
                if not _is_transient_disconnect(exc):
                    logger.exception(
                        "Worker OCR failed page {}: {}", job.page_num, exc
                    )
                    await _finish_page_job(job.id, status="failed", error=str(exc))
                    return
                last_err = exc
                wait_s = 8 * (attempt + 1)
                logger.warning(
                    "Worker page {}: disconnect ({}); retry {}/2 in {}s",
                    job.page_num,
                    exc,
                    attempt + 1,
                    wait_s,
                )
                await vl_limiter.on_transient_failure()
                await asyncio.sleep(wait_s)

        if page_blocks is None:
            logger.error(
                "Worker page {}: giving up after disconnects: {}",
                job.page_num,
                last_err,
            )
            page_blocks = _skipped_connection_blocks(int(job.page_num))

        if job.keep_page_images:
            pages_dir = doc_dir(job.document_id) / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            dest = pages_dir / f"page-{int(job.page_num):04d}.png"
            try:
                shutil.copy2(img_path, dest)
            except Exception as exc:
                logger.warning("keep_page_images copy failed: {}", exc)

        await _finish_page_job(
            job.id,
            status="done",
            result_json=_blocks_to_json(page_blocks),
        )


def _worker_process_main(
    worker_id: str,
    worker_count: int,
    shared_state: SharedVLLimiterState,
    stop_event: Any,
    cancel_dict: Any,
) -> None:
    """Entry point for a spawned OCR worker process."""
    try:
        asyncio.run(
            _worker_async_main(
                worker_id, worker_count, shared_state, stop_event, cancel_dict
            )
        )
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("OCR worker {} crashed", worker_id)


async def _worker_async_main(
    worker_id: str,
    worker_count: int,
    shared_state: SharedVLLimiterState,
    stop_event: Any,
    cancel_dict: Any,
) -> None:
    from ocr_app.db.session import create_tables, init_engine
    from ocr_app.ocr_core.dashscope_http_pool import (
        close_dashscope_http_pool,
        install_dashscope_http_pool,
    )
    from ocr_app.settings_store import bootstrap_from_disk

    bootstrap_from_disk()
    init_engine(force=True)
    await create_tables()
    set_vl_limiter(MultiprocessVLLimiter(shared_state))
    await install_dashscope_http_pool(force=True, worker_count=worker_count)
    logger.info(
        "OCR worker {} ready (workers={} page_conc~{})",
        worker_id,
        worker_count,
        max(1, int(settings.ocr_page_concurrency) // worker_count),
    )

    local_limit = max(1, int(settings.ocr_page_concurrency) // worker_count)
    inflight: set[asyncio.Task[None]] = set()

    try:
        while not stop_event.is_set():
            done = {t for t in inflight if t.done()}
            for t in done:
                try:
                    t.result()
                except Exception:
                    logger.exception("Worker task error in {}", worker_id)
            inflight -= done

            while len(inflight) < local_limit and not stop_event.is_set():
                job = await _claim_next_page_job(worker_id)
                if job is None:
                    # Empty or all docs at momentary race; don't busy-spin.
                    break
                if cancel_dict.get(job.document_id):
                    await _finish_page_job(job.id, status="cancelled")
                    continue
                task = asyncio.create_task(
                    _process_claimed_page(job, cancel_dict=cancel_dict),
                    name=f"{worker_id}-p{job.page_num}",
                )
                inflight.add(task)

            if not inflight:
                await asyncio.sleep(0.25)
            else:
                await asyncio.wait(
                    inflight, timeout=0.5, return_when=asyncio.FIRST_COMPLETED
                )
    finally:
        for t in inflight:
            if not t.done():
                t.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await close_dashscope_http_pool()
        logger.info("OCR worker {} stopped", worker_id)


class OcrWorkerPool:
    """Coordinator-side manager for OCR worker child processes."""

    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")
        self._processes: list[mp.Process] = []
        self._stop_event: Any | None = None
        self._cancel_dict: Any | None = None
        self._manager: Any | None = None
        self._shared: SharedVLLimiterState | None = None
        self._worker_count = 0

    @property
    def active(self) -> bool:
        return self._worker_count > 1 and bool(self._processes)

    @property
    def worker_count(self) -> int:
        return self._worker_count

    @property
    def shared_limiter(self) -> SharedVLLimiterState | None:
        return self._shared

    def request_cancel(self, document_id: str) -> None:
        if self._cancel_dict is not None:
            self._cancel_dict[document_id] = True

    def clear_cancel(self, document_id: str) -> None:
        if self._cancel_dict is not None:
            try:
                del self._cancel_dict[document_id]
            except KeyError:
                pass

    async def start(self) -> None:
        desired = max(1, int(settings.ocr_worker_processes))
        # PyInstaller + multiprocessing.Manager often hangs on Windows; keep
        # the HTTP server responsive by forcing in-process OCR in frozen builds.
        if desired > 1 and _is_frozen():
            logger.warning(
                "Frozen sidecar: ignoring ocr_worker_processes={} (in-process mode)",
                desired,
            )
            desired = 1
        if desired <= 1:
            self._worker_count = 1
            logger.info("OCR worker pool idle (ocr_worker_processes=1, in-process mode)")
            return
        await self._spawn(desired)

    async def stop(self) -> None:
        if self._processes:
            logger.info("Stopping {} OCR worker process(es)", len(self._processes))
            if self._stop_event is not None:
                self._stop_event.set()

            def _join_all() -> None:
                for proc in list(self._processes):
                    proc.join(timeout=15)
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(timeout=5)

            await asyncio.to_thread(_join_all)
            self._processes.clear()
        if self._manager is not None:
            try:
                self._manager.shutdown()
            except Exception:
                pass
            self._manager = None
        self._stop_event = None
        self._cancel_dict = None
        self._shared = None
        self._worker_count = 1
        set_vl_limiter(AdaptiveVLLimiter())
        if max(1, int(settings.ocr_worker_processes)) <= 1:
            logger.info("OCR worker pool idle (in-process mode)")
        else:
            logger.info("OCR worker pool stopped")

    async def reconfigure(self) -> None:
        desired = max(1, int(settings.ocr_worker_processes))
        if desired > 1 and _is_frozen():
            desired = 1
        api_conc = max(1, int(settings.ocr_api_concurrency))
        if self._shared is not None:
            self._shared.reconfigure(api_conc)
        if desired == self._worker_count and (
            desired <= 1 or (self._processes and all(p.is_alive() for p in self._processes))
        ):
            return
        await self.stop()
        if desired > 1:
            await self._spawn(desired)
        else:
            self._worker_count = 1
            logger.info("OCR worker pool idle (ocr_worker_processes=1)")

    async def _spawn(self, worker_count: int) -> None:
        await heal_stale_page_jobs()
        api_conc = max(1, int(settings.ocr_api_concurrency))
        self._manager = self._ctx.Manager()
        self._cancel_dict = self._manager.dict()
        self._stop_event = self._ctx.Event()
        self._shared = SharedVLLimiterState(self._ctx, configured_max=api_conc)
        set_vl_limiter(MultiprocessVLLimiter(self._shared))

        from ocr_app.ocr_core.dashscope_http_pool import install_dashscope_http_pool

        await install_dashscope_http_pool(force=True, worker_count=worker_count)

        self._processes = []
        for i in range(worker_count):
            wid = f"ocr-worker-{i}"
            proc = self._ctx.Process(
                target=_worker_process_main,
                name=wid,
                args=(
                    wid,
                    worker_count,
                    self._shared,
                    self._stop_event,
                    self._cancel_dict,
                ),
                daemon=True,
            )
            proc.start()
            self._processes.append(proc)
        self._worker_count = worker_count
        logger.info(
            "OCR worker pool started ({} process(es), api_cap={})",
            worker_count,
            api_conc,
        )


ocr_worker_pool = OcrWorkerPool()


async def wait_for_page_jobs(
    *,
    job_id: str,
    pages: list[int],
    document_id: str,
    on_page_done,
    should_abort,
    poll_s: float = 0.2,
) -> dict[int, list[Any]]:
    """Poll DB until all pages are terminal; invoke on_page_done for each done page."""
    pending = set(int(p) for p in pages)
    known_done: set[int] = set()
    results: dict[int, list[Any]] = {}
    failures: list[str] = []

    while pending:
        if await should_abort():
            ocr_worker_pool.request_cancel(document_id)
            await cancel_page_jobs_for_job(job_id)
            raise asyncio.CancelledError()

        rows = await fetch_page_job_updates(job_id, known_done=known_done)
        for row in rows:
            pnum = int(row.page_num)
            if pnum in known_done:
                continue
            known_done.add(pnum)
            pending.discard(pnum)
            if row.status == "done":
                blocks = blocks_from_page_result(row.result_json)
                results[pnum] = blocks
                await on_page_done(pnum, blocks)
            elif row.status == "cancelled":
                raise asyncio.CancelledError()
            else:
                failures.append(f"page {pnum}: {row.error or row.status}")
                ocr_worker_pool.request_cancel(document_id)
                await cancel_page_jobs_for_job(job_id)
                raise RuntimeError("; ".join(failures[:5]))

        if not pending:
            break
        await asyncio.sleep(poll_s)

    return results
